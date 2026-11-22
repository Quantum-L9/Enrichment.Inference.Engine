"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: enrichment
layer: [engines]
tags: [enrichment-sweep, readiness, budget, converge-request-v2]
owner: engine-team
status: active
--- /L9_META ---

Enrichment sweep (docs/contracts/enrichment-sweep.md).

EIE owns the enrichment trigger. A consumer hands EIE candidate records and a
daily budget; EIE asks the graph engine (through Gate, action `health-assess`)
how ready each record is, ranks the ones with gaps, and runs the canonical
converge path on as many as the budget allows. Results are kept for the
consumer to review; nothing here writes to the consumer.

Shared code only. The tenant comes from the packet, the node label and every
field name from the tenant's own cartridge or from the graph engine's answer.
Nothing here names a tenant, a domain, a node or a field.

Fail closed, everywhere it matters:

* readiness that is disabled, refused, failed, malformed or simply absent for a
  candidate means that candidate is not enriched. There is no local readiness
  guess standing in for the graph engine's (L9-DEGRADE-001);
* a budget ledger that cannot be read or written means nothing more is
  enriched;
* a converge that raises is charged its full reservation, because what it spent
  is unknown (L9-UNKNOWN-001).
"""

from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import structlog

from ..services.sweep_store import RUNNING, SWEEP_FAILED, SweepRecord, SweepStore
from .converge_request_v2 import REQUEST_CONTRACT_V2
from .domain_yaml_reader import DomainEnrichmentConfig, default_node_label
from .spec_types import node_properties

logger = structlog.get_logger("enrichment_sweep")
# The structured-log event of every "budget ledger cannot be read or written" stop.
_LEDGER_UNAVAILABLE_EVENT = "enrichment_sweep.ledger_unavailable"

ASSESS_ACTION = "health-assess"
ASSESS_SUBACTION = "health_batch_assess"
# CEG's batch limit for health_batch_assess (max_entities <= 100).
ASSESS_BATCH_LIMIT = 100
_MAX_ID_LENGTH = 256
_MONEY = Decimal("0.000001")
# A batch reply status that is not one of these is a refusal. An absent status
# is read as an answer: the reply is then judged by its `entities` list alone.
_ANSWER_STATUSES = frozenset({"success", "ok", "completed", "partial"})
_CANDIDATE_KEYS = frozenset({"entity_id", "node_label", "entity", "field_confidences"})
_REQUEST_KEYS = frozenset({"candidates", "daily_budget_usd", "idempotency_key"})
_SWEEP_OBJECTIVE = "Close the readiness gaps the graph engine reported for this record"

AssessFn = Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any]]]
ConvergeFn = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
LoadCartridgeFn = Callable[[str], DomainEnrichmentConfig]


class SweepRefusedError(ValueError):
    """A sweep request refused whole. `code` is the contract code (section 6)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ── Identity and money ─────────────────────────────────────────────────────


def sweep_id_for(tenant: str, idempotency_key: str) -> str:
    """The sweep id: deterministic per (tenant, idempotency_key)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"eie:enrichment-sweep:{tenant}:{idempotency_key}").hex


def converge_idempotency_key(sweep_id: str, entity_id: str) -> str:
    """The converge idempotency key: deterministic per (sweep_id, entity_id)."""
    digest = hashlib.sha256(f"{sweep_id}\x1f{entity_id}".encode()).hexdigest()
    return f"sweep-{digest}"


def money(value: float | int | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(_MONEY, rounding=ROUND_HALF_UP)


def effective_budget(requested: Decimal, ceiling: float) -> Decimal:
    """min(request, deployment ceiling): the consumer can lower the cap, never raise it."""
    return min(money(requested), money(ceiling))


def reservation_estimate(settings: Any) -> Decimal:
    """The most one converge can spend: its token ceiling at the configured rate."""
    return money(settings.max_budget_tokens * settings.token_rate_usd_per_1k / 1000)


def actual_cost(response: Mapping[str, Any], settings: Any, estimate: Decimal) -> Decimal:
    """What a converge spent, from its `tokens_used`; unknown spend costs the estimate."""
    tokens = response.get("tokens_used")
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
        return estimate
    return money(tokens * settings.token_rate_usd_per_1k / 1000)


def utc_day(now: datetime) -> date:
    return now.astimezone(UTC).date()


# ── Request validation ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ValidatedRequest:
    """A sweep request that passed envelope validation, split into accepted and rejected."""

    idempotency_key: str
    requested_budget_usd: Decimal
    accepted: tuple[dict[str, Any], ...]
    rejected: tuple[dict[str, Any], ...]


def validate_request(
    payload: Mapping[str, Any],
    config: DomainEnrichmentConfig,
    max_candidates: int,
) -> ValidatedRequest:
    """Validate a sweep request against the tenant's cartridge.

    The request as a whole is refused (`SweepRefusedError`) when its envelope is
    wrong. Individual candidates are rejected with a reason and the rest go on.
    """
    key, budget, candidates = _envelope(payload)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(candidates):
        entity_id = raw.get("entity_id") if isinstance(raw, Mapping) else None
        label = entity_id if isinstance(entity_id, str) and entity_id else f"#{index}"
        if len(accepted) >= max_candidates:
            rejected.append({"entity_id": label, "reason": "candidate_cap_exceeded"})
            continue
        reason, candidate = _candidate(raw, config)
        if candidate is not None and candidate["entity_id"] in seen:
            reason, candidate = "duplicate_entity_id", None
        if candidate is None:
            rejected.append({"entity_id": label, "reason": reason})
            continue
        seen.add(candidate["entity_id"])
        accepted.append(candidate)
    return ValidatedRequest(
        idempotency_key=key,
        requested_budget_usd=budget,
        accepted=tuple(accepted),
        rejected=tuple(rejected),
    )


def _envelope(payload: object) -> tuple[str, Decimal, list[Any]]:
    """The request envelope: (idempotency_key, requested budget, raw candidates)."""
    if not isinstance(payload, Mapping):
        raise SweepRefusedError("invalid_request", "payload must be an object")
    unknown = sorted(str(k) for k in payload.keys() - _REQUEST_KEYS)
    if unknown:
        raise SweepRefusedError("invalid_request", f"unknown members {unknown}")
    key = payload.get("idempotency_key")
    if not isinstance(key, str) or not key.strip() or len(key) > _MAX_ID_LENGTH:
        raise SweepRefusedError(
            "invalid_request", f"idempotency_key must be a nonempty string <= {_MAX_ID_LENGTH}"
        )
    budget = payload.get("daily_budget_usd")
    if (
        isinstance(budget, bool)
        or not isinstance(budget, int | float)
        or not math.isfinite(budget)
        or budget <= 0
    ):
        raise SweepRefusedError("invalid_request", "daily_budget_usd must be a positive number")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise SweepRefusedError("invalid_request", "candidates must be a nonempty list")
    return key, money(budget), candidates


def _candidate(raw: object, config: DomainEnrichmentConfig) -> tuple[str, dict[str, Any] | None]:
    if not isinstance(raw, Mapping):
        return "invalid_candidate", None
    if raw.keys() - _CANDIDATE_KEYS:
        return "invalid_candidate", None
    entity_id = raw.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id.strip() or len(entity_id) > _MAX_ID_LENGTH:
        return "invalid_entity_id", None
    entity = raw.get("entity")
    if not isinstance(entity, Mapping):
        return "invalid_entity", None
    confidences = raw.get("field_confidences", {})
    if confidences is None:
        confidences = {}
    if not isinstance(confidences, Mapping) or not all(
        isinstance(name, str)
        and not isinstance(value, bool)
        and isinstance(value, int | float)
        and 0.0 <= value <= 1.0
        for name, value in confidences.items()
    ):
        return "invalid_field_confidences", None
    node_label = raw.get("node_label")
    if node_label is None:
        node_label = default_node_label(config)
        if node_label is None:
            return "node_label_required", None
    if not isinstance(node_label, str) or node_label not in config.node_schemas:
        return "node_not_declared", None
    if node_properties(config.raw_spec, node_label) is None:
        return "node_not_declared", None
    return "", {
        "entity_id": entity_id,
        "node_label": node_label,
        "entity": dict(entity),
        "field_confidences": dict(confidences),
    }


# ── Readiness (graph engine, through Gate) ─────────────────────────────────


@dataclass(frozen=True)
class Readiness:
    """The part of CEG's entity_health a sweep reads. Nothing else is consumed."""

    entity_id: str
    critical_gaps: tuple[str, ...]
    enrichment_targets: tuple[str, ...]
    readiness_score: float | None
    grade: Any
    gate_completeness: Any
    batch: int
    position: int

    @property
    def has_gaps(self) -> bool:
        return bool(self.critical_gaps or self.enrichment_targets)


def batches(candidates: Sequence[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    for start in range(0, len(candidates), ASSESS_BATCH_LIMIT):
        yield list(candidates[start : start + ASSESS_BATCH_LIMIT])


def assessment_payload(batch: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """CEG health_batch_assess body. `data`, never `entity_data`; no domain, no tier."""
    if len(batch) > ASSESS_BATCH_LIMIT:
        raise ValueError(f"a readiness batch holds at most {ASSESS_BATCH_LIMIT} entities")
    return {
        "subaction": ASSESS_SUBACTION,
        "entities": [
            {
                "entity_id": c["entity_id"],
                "node_label": c["node_label"],
                "data": c["entity"],
                "field_confidences": c["field_confidences"],
            }
            for c in batch
        ],
        "max_entities": len(batch),
    }


def parse_assessment(
    reply: object, batch: Sequence[dict[str, Any]], batch_index: int
) -> tuple[dict[str, Readiness], list[dict[str, Any]]]:
    """Readiness per candidate of `batch`, and a skip for every candidate without one."""
    requested = [c["entity_id"] for c in batch]
    if not isinstance(reply, Mapping):
        return {}, _skip_all(requested, "readiness_malformed")
    status = reply.get("status")
    if status == "disabled":
        return {}, _skip_all(requested, "readiness_disabled")
    if status is not None and status not in _ANSWER_STATUSES:
        return {}, _skip_all(requested, "readiness_refused")
    entities = reply.get("entities")
    if not isinstance(entities, list):
        return {}, _skip_all(requested, "readiness_malformed")

    wanted = set(requested)
    found: dict[str, Readiness] = {}
    malformed: set[str] = set()
    for position, entry in enumerate(entities):
        if not isinstance(entry, Mapping):
            continue
        entity_id = entry.get("entity_id")
        if not isinstance(entity_id, str) or entity_id not in wanted or entity_id in found:
            continue
        readiness = _readiness(entry.get("entity_health"), entity_id, batch_index, position)
        if readiness is None:
            malformed.add(entity_id)
        else:
            found[entity_id] = readiness
    skipped = [
        {
            "entity_id": eid,
            "reason": "readiness_malformed" if eid in malformed else "readiness_absent",
        }
        for eid in requested
        if eid not in found
    ]
    return found, skipped


def _readiness(health: object, entity_id: str, batch: int, position: int) -> Readiness | None:
    if not isinstance(health, Mapping):
        return None
    gaps = _field_names(health.get("critical_gaps", []))
    targets = _field_names(health.get("enrichment_targets", []))
    if gaps is None or targets is None:
        return None
    score = health.get("readiness_score")
    if score is not None and (isinstance(score, bool) or not isinstance(score, int | float)):
        return None
    return Readiness(
        entity_id=entity_id,
        critical_gaps=gaps,
        enrichment_targets=targets,
        readiness_score=None if score is None else _as_float(score),
        grade=health.get("grade"),
        gate_completeness=health.get("gate_completeness"),
        batch=batch,
        position=position,
    )


def _as_float(value: int | float) -> float | None:
    """A finite float, or None when it cannot be represented as one."""
    try:
        return float(value)
    except (ValueError, TypeError, OverflowError):
        return None


def _field_names(value: object) -> tuple[str, ...] | None:
    """A list of field names, in CEG's shapes (engine/health/field_health.py).

    `critical_gaps` is a list of field-name strings. `enrichment_targets` is a
    list of EnrichmentTarget objects whose field is named `field_name`. Any
    other shape is malformed: the candidate is skipped, never guessed at.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        return None
    names: list[str] = []
    for item in value:
        name = item.get("field_name") if isinstance(item, Mapping) else item
        if not isinstance(name, str) or not name:
            return None
        names.append(name)
    return tuple(names)


def _skip_all(entity_ids: Sequence[str], reason: str) -> list[dict[str, Any]]:
    return [{"entity_id": eid, "reason": reason} for eid in entity_ids]


def rank(readiness: Sequence[Readiness]) -> list[Readiness]:
    """Critical gaps first, then CEG's order, then lower readiness_score.

    CEG orders each batch reply; `position` is that order. Across batches the
    positions coincide, and readiness_score breaks the tie. A missing score
    sorts last within its tier. Only candidates with gaps are ranked.
    """
    return sorted(
        (r for r in readiness if r.has_gaps),
        key=lambda r: (
            0 if r.critical_gaps else 1,
            r.position,
            r.readiness_score if r.readiness_score is not None else math.inf,
            r.batch,
            r.entity_id,
        ),
    )


def targets_for(readiness: Readiness, declared: set[str]) -> tuple[list[str], list[str]]:
    """Critical gaps then enrichment targets, deduplicated; undeclared names are dropped."""
    targets: list[str] = []
    dropped: list[str] = []
    for name in (*readiness.critical_gaps, *readiness.enrichment_targets):
        if name in targets or name in dropped:
            continue
        (targets if name in declared else dropped).append(name)
    return targets, dropped


def converge_body(
    tenant: str,
    config: DomainEnrichmentConfig,
    candidate: Mapping[str, Any],
    targets: Sequence[str],
    sweep_id: str,
) -> dict[str, Any]:
    """A converge request v2 for one candidate (docs/contracts/converge/request.v2.md).

    The cartridge is the tenant's own (tenant id == cartridge id) pinned to the
    revision the targets were checked against, so a revision change between
    readiness and converge is refused by converge rather than run.
    """
    cartridge: dict[str, Any] = {"id": tenant, "version": config.version}
    if config.source_sha256:
        cartridge["sha256"] = config.source_sha256
    return {
        "contract": REQUEST_CONTRACT_V2,
        "cartridge": cartridge,
        "node": candidate["node_label"],
        "targets": [{"field": name} for name in targets],
        "entity": dict(candidate["entity"]),
        "entity_id": candidate["entity_id"],
        "object_type": candidate["node_label"],
        "objective": _SWEEP_OBJECTIVE,
        "idempotency_key": converge_idempotency_key(sweep_id, candidate["entity_id"]),
    }


# ── The background run ─────────────────────────────────────────────────────


@dataclass
class _Run:
    skipped: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, int] = field(
        default_factory=lambda: {"enriched": 0, "targets_dropped_undeclared": 0}
    )
    enriched: list[str] = field(default_factory=list)
    seq: int = 0

    def skip(self, entity_id: str, reason: str) -> None:
        self.skipped.append({"entity_id": entity_id, "reason": reason})
        self.stats[reason] = self.stats.get(reason, 0) + 1


class SweepRunner:
    """Runs one accepted sweep to completion. Every collaborator is injected."""

    def __init__(
        self,
        *,
        store: SweepStore,
        assess: AssessFn,
        converge: ConvergeFn,
        load_cartridge: LoadCartridgeFn,
        settings: Any,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._assess = assess
        self._converge = converge
        self._load = load_cartridge
        self._settings = settings
        self._clock = clock

    async def run(self, record: SweepRecord) -> None:
        run = _Run()
        try:
            await self._run(record, run)
        except Exception as exc:  # the sweep must still end, and say why
            logger.exception("enrichment_sweep.failed", sweep_id=record.sweep_id, error=str(exc))
            done = {s["entity_id"] for s in run.skipped} | set(run.enriched)
            for candidate in record.candidates:
                if candidate["entity_id"] not in done:
                    run.skip(candidate["entity_id"], "sweep_failed")
        try:
            await self._store.save_progress(
                record.sweep_id, run.skipped, run.stats, completed_at=self._clock()
            )
        except Exception as exc:
            logger.error(
                "enrichment_sweep.completion_not_recorded", sweep_id=record.sweep_id, error=str(exc)
            )
        logger.info(
            "enrichment_sweep.completed",
            sweep_id=record.sweep_id,
            tenant=record.tenant_id,
            stats=run.stats,
        )

    async def _run(self, record: SweepRecord, run: _Run) -> None:
        try:
            config = self._load(record.tenant_id)
        except Exception as exc:
            logger.warning(
                "enrichment_sweep.cartridge_unavailable", tenant=record.tenant_id, error=str(exc)
            )
            for candidate in record.candidates:
                run.skip(candidate["entity_id"], "cartridge_unavailable")
            return

        assessed = await self._assess_all(record, run)
        for readiness in assessed:
            if not readiness.has_gaps:
                run.skip(readiness.entity_id, "no_readiness_gaps")
        ranked = rank(assessed)

        candidates = {c["entity_id"]: c for c in record.candidates}
        estimate = reservation_estimate(self._settings)
        for index, readiness in enumerate(ranked):
            candidate = candidates[readiness.entity_id]
            targets = self._declared_targets(record, config, candidate, readiness, run)
            if not targets:
                run.skip(readiness.entity_id, "no_declared_targets")
                continue
            stop = await self._enrich_one(record, config, candidate, targets, estimate, run)
            if stop is not None:
                for remaining in ranked[index:]:
                    run.skip(remaining.entity_id, stop)
                return
            await self._save(record, run)

    async def _assess_all(self, record: SweepRecord, run: _Run) -> list[Readiness]:
        """CEG readiness for every candidate, batch by batch; the rest are skipped."""
        assessed: list[Readiness] = []
        for batch_index, batch in enumerate(batches(record.candidates)):
            key = f"{record.sweep_id}:assess:{batch_index}"
            try:
                reply = await self._assess(record.tenant_id, assessment_payload(batch), key)
            except Exception as exc:
                logger.warning(
                    "enrichment_sweep.readiness_failed",
                    sweep_id=record.sweep_id,
                    batch=batch_index,
                    error=f"{type(exc).__name__}: {exc}",
                )
                for candidate in batch:
                    run.skip(candidate["entity_id"], "readiness_failed")
                continue
            found, skipped = parse_assessment(reply, batch, batch_index)
            for skip in skipped:
                run.skip(skip["entity_id"], skip["reason"])
            if isinstance(reply, Mapping):
                partial = {
                    flag: reply.get(flag)
                    for flag in ("total_truncated", "cost_ceiling_reached", "total_skipped")
                    if reply.get(flag)
                }
                if partial:
                    logger.info(
                        "enrichment_sweep.readiness_partial",
                        sweep_id=record.sweep_id,
                        batch=batch_index,
                        **partial,
                    )
            assessed.extend(found[c["entity_id"]] for c in batch if c["entity_id"] in found)
        return assessed

    @staticmethod
    def _declared_targets(
        record: SweepRecord,
        config: DomainEnrichmentConfig,
        candidate: Mapping[str, Any],
        readiness: Readiness,
        run: _Run,
    ) -> list[str]:
        """CEG's targets the candidate's node declares; the others are counted and dropped."""
        properties = node_properties(config.raw_spec, candidate["node_label"]) or []
        targets, dropped = targets_for(readiness, {p["name"] for p in properties})
        if dropped:
            run.stats["targets_dropped_undeclared"] += len(dropped)
            logger.info(
                "enrichment_sweep.targets_dropped_undeclared",
                sweep_id=record.sweep_id,
                entity_id=readiness.entity_id,
                dropped=dropped,
            )
        return targets

    async def _enrich_one(
        self,
        record: SweepRecord,
        config: DomainEnrichmentConfig,
        candidate: Mapping[str, Any],
        targets: Sequence[str],
        estimate: Decimal,
        run: _Run,
    ) -> str | None:
        """Reserve, converge, settle, store. Returns a stop reason, or None to go on."""
        stop = await self._still_running(record)
        if stop is not None:
            return stop
        stop = await self._reserve(record, estimate, record.effective_budget_usd)
        if stop is not None:
            return stop
        entity_id = candidate["entity_id"]
        body = converge_body(record.tenant_id, config, candidate, targets, record.sweep_id)
        response: dict[str, Any] | None
        try:
            response = await self._converge(record.tenant_id, body)
            cost = actual_cost(response, self._settings, estimate)
        except Exception as exc:
            logger.exception(
                "enrichment_sweep.converge_failed",
                sweep_id=record.sweep_id,
                entity_id=entity_id,
                error=str(exc),
            )
            response, cost = None, estimate
        try:
            await self._store.settle(record.tenant_id, record.day, estimate, cost)
            if response is not None:
                run.seq += 1
                await self._store.add_result(record.sweep_id, run.seq, entity_id, response, cost)
        except Exception as exc:
            logger.error(_LEDGER_UNAVAILABLE_EVENT, sweep_id=record.sweep_id, error=str(exc))
            return "budget_ledger_unavailable"
        if response is None:
            run.skip(entity_id, "converge_failed")
        else:
            run.stats["enriched"] += 1
            run.enriched.append(entity_id)
        return None

    async def _still_running(self, record: SweepRecord) -> str | None:
        """None while the sweep is running; a stop reason once it was closed.

        A sweep closed as stale (its worker looked dead) stops here, before
        reserving anything: a closed sweep never spends again.
        """
        try:
            current = await self._store.get_sweep(record.tenant_id, record.sweep_id)
        except Exception as exc:
            logger.error(_LEDGER_UNAVAILABLE_EVENT, sweep_id=record.sweep_id, error=str(exc))
            return "budget_ledger_unavailable"
        if current is None or current.state != RUNNING:
            logger.warning("enrichment_sweep.closed_while_running", sweep_id=record.sweep_id)
            return SWEEP_FAILED
        return None

    async def _reserve(self, record: SweepRecord, estimate: Decimal, cap: Decimal) -> str | None:
        """None when reserved; otherwise the reason every remaining candidate is skipped."""
        try:
            granted = await self._store.reserve(record.tenant_id, record.day, estimate, cap)
        except Exception as exc:
            logger.error(_LEDGER_UNAVAILABLE_EVENT, sweep_id=record.sweep_id, error=str(exc))
            return "budget_ledger_unavailable"
        return None if granted else "budget_exhausted"

    async def _save(self, record: SweepRecord, run: _Run) -> None:
        try:
            await self._store.save_progress(record.sweep_id, run.skipped, run.stats)
        except Exception as exc:
            logger.warning(
                "enrichment_sweep.progress_not_recorded", sweep_id=record.sweep_id, error=str(exc)
            )


__all__ = [
    "ASSESS_ACTION",
    "ASSESS_BATCH_LIMIT",
    "Readiness",
    "SweepRefusedError",
    "SweepRunner",
    "ValidatedRequest",
    "actual_cost",
    "assessment_payload",
    "batches",
    "converge_body",
    "converge_idempotency_key",
    "effective_budget",
    "money",
    "parse_assessment",
    "rank",
    "reservation_estimate",
    "sweep_id_for",
    "targets_for",
    "utc_day",
    "validate_request",
]
