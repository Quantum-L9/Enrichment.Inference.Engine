"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: enrichment
layer: [engines]
tags: [enrichment-sweep, handlers]
owner: engine-team
status: active
--- /L9_META ---

SDK handlers for `enrichment-sweep` and `enrichment-sweep-results`
(docs/contracts/enrichment-sweep.md).

`enrichment-sweep` validates, records the sweep and answers at once; the run
continues in the background. `enrichment-sweep-results` pages what a sweep
produced. With `l9_enrichment_sweep_enabled` off, both refuse with `disabled`.

Collaborators are the existing ones, never forks: readiness goes to Gate
through the one PacketRouter (`get_router(...).route`, action `health-assess`),
each enrichment through the public canonical converge handler
(`handlers.handle_converge`), and the cartridge comes from the same reader the
converge handlers were initialised with.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from ..core.config import get_settings
from ..services.sweep_store import (
    COMPLETED,
    RUNNING,
    LedgerState,
    PgSweepStore,
    SweepRecord,
    SweepResultRow,
    SweepStore,
)
from .domain_yaml_reader import DomainEnrichmentConfig, DomainYamlReader
from .enrichment_sweep import (
    ASSESS_ACTION,
    AssessFn,
    ConvergeFn,
    SweepRefusedError,
    SweepRunner,
    ValidatedRequest,
    effective_budget,
    money,
    sweep_id_for,
    utc_day,
    validate_request,
)

logger = structlog.get_logger("sweep_handlers")

_RESULTS_KEYS = frozenset({"sweep_id", "cursor"})

#: A running sweep nothing has touched for this long -- no new result, no
#: ledger write of its tenant-day -- has lost its worker (a restart, a crash).
#: Far above the longest gap a live sweep leaves: readiness is at most
#: ceil(500 / 100) Gate calls before the first reservation, and every converge
#: is bounded by the canonical 25 s budget and touches the ledger twice.
SWEEP_STALE_AFTER = timedelta(minutes=30)
#: How often this process looks for such sweeps, with no request needed.
SWEEP_STALE_POLL_SECONDS = 60.0


@dataclass
class _Wiring:
    reader: DomainYamlReader | None = None
    store: SweepStore | None = None
    assess: AssessFn | None = None
    converge: ConvergeFn | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


_wiring = _Wiring()
_background: set[asyncio.Task[None]] = set()
# Sweeps whose worker is a task of this process: never stale while it runs.
_running_here: set[str] = set()
_stale_poll: asyncio.Task[None] | None = None


def init_sweep(
    *,
    reader: DomainYamlReader | None,
    store: SweepStore | None = None,
    assess: AssessFn | None = None,
    converge: ConvergeFn | None = None,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Wire the sweep. Unset collaborators resolve to the production ones on use."""
    global _wiring
    _wiring = _Wiring(
        reader=reader,
        store=store,
        assess=assess,
        converge=converge,
        clock=clock or (lambda: datetime.now(UTC)),
    )
    if store is None:
        # The production wiring: the durable store, where a sweep outlives the
        # process that started it. An injected store is a caller's own.
        start_stale_sweep_poll()


def start_stale_sweep_poll(interval_seconds: float = SWEEP_STALE_POLL_SECONDS) -> bool:
    """Close stale sweeps every `interval_seconds` in this process (startup).

    Started from `init_sweep`, which startup registration runs inside the event
    loop. Returns False, starting nothing, outside a running loop or when the
    poll already runs on it. A pass that finds the store unavailable is logged
    and retried on the next tick.
    """
    global _stale_poll
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    current = _stale_poll
    if current is not None and not current.done() and current.get_loop() is loop:
        return False
    _stale_poll = loop.create_task(_stale_loop(interval_seconds), name="enrichment-sweep:stale")
    return True


async def stop_stale_sweep_poll() -> None:
    """Stop this process's stale-sweep poll (shutdown, tests)."""
    global _stale_poll
    task, _stale_poll = _stale_poll, None
    if task is None or task.get_loop().is_closed():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _stale_loop(interval_seconds: float) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        await close_stale_sweeps()


async def close_stale_sweeps() -> list[str]:
    """Complete every running sweep whose worker is gone; never raises.

    Its unhandled candidates are skipped `sweep_failed` and the ledger is left
    as it is, so no reservation is released into a second spend and the cap
    holds. Sweeps this process is running are never closed.
    """
    try:
        closed = await _store().close_stale_sweeps(
            _wiring.clock(), SWEEP_STALE_AFTER, exclude=set(_running_here)
        )
    except Exception as exc:
        logger.warning("enrichment_sweep.stale_close_failed", error=str(exc))
        return []
    if closed:
        logger.warning("enrichment_sweep.stale_closed", sweeps=closed)
    return closed


async def wait_for_background_sweeps() -> None:
    """Await every sweep still running in this process (shutdown, tests)."""
    while _background:
        await asyncio.gather(*list(_background), return_exceptions=True)


def _store() -> SweepStore:
    return _wiring.store if _wiring.store is not None else PgSweepStore()


async def _gate_assess(
    tenant: str, payload: dict[str, Any], idempotency_key: str
) -> dict[str, Any]:
    from .packet_router import NodeTarget, get_router

    return await get_router(get_settings()).route(
        NodeTarget.GRAPH, ASSESS_ACTION, tenant, payload, idempotency_key=idempotency_key
    )


async def _canonical_converge(tenant: str, body: dict[str, Any]) -> dict[str, Any]:
    from .handlers import handle_converge

    return await handle_converge(tenant, body)


def _load_cartridge(tenant: str) -> DomainEnrichmentConfig:
    reader = _wiring.reader
    if reader is None:
        raise SweepRefusedError("cartridge_unavailable", "no cartridge reader is configured")
    try:
        return reader.load(tenant)
    except SweepRefusedError:
        raise
    except Exception as exc:
        raise SweepRefusedError("cartridge_unavailable", f"{tenant}: {exc}") from exc


def _require_enabled(settings: Any) -> None:
    if not settings.l9_enrichment_sweep_enabled:
        raise SweepRefusedError(
            "disabled", "enrichment sweep is disabled (L9_ENRICHMENT_SWEEP_ENABLED is false)"
        )


async def handle_enrichment_sweep(
    tenant: str,
    payload: dict[str, Any],
    packet: Any = None,
) -> dict[str, Any]:
    """Accept a sweep and start it. Answers {sweep_id, accepted, rejected} at once."""
    settings = get_settings()
    _require_enabled(settings)
    config = _load_cartridge(tenant)
    request = validate_request(payload, config, settings.l9_sweep_max_candidates_per_request)
    sweep_id = sweep_id_for(tenant, request.idempotency_key)
    accepted_ids = [c["entity_id"] for c in request.accepted]
    now = _wiring.clock()
    record = _new_record(tenant, sweep_id, request, settings, now)
    store = _store()
    await _purge(store, settings, now)
    await close_stale_sweeps()
    try:
        stored, created = await store.create_sweep(record)
    except Exception as exc:
        logger.error("enrichment_sweep.store_unavailable", tenant=tenant, error=str(exc))
        raise SweepRefusedError("sweep_store_unavailable", str(exc)) from exc
    if not created:
        # Same (tenant, idempotency_key): the original answer, no new work and
        # no new reservation, whatever the replayed body says.
        return {
            "sweep_id": stored.sweep_id,
            "accepted": [c["entity_id"] for c in stored.candidates],
            "rejected": list(stored.rejected),
        }

    if stored.state != COMPLETED:
        _start(store, stored, settings)
    logger.info(
        "enrichment_sweep.accepted",
        tenant=tenant,
        sweep_id=sweep_id,
        accepted=len(accepted_ids),
        rejected=len(request.rejected),
        effective_budget_usd=str(record.effective_budget_usd),
    )
    return {"sweep_id": sweep_id, "accepted": accepted_ids, "rejected": list(request.rejected)}


def _new_record(
    tenant: str, sweep_id: str, request: ValidatedRequest, settings: Any, now: datetime
) -> SweepRecord:
    """The sweep to persist. One with nothing accepted is recorded already completed,
    so the key -> sweep_id mapping and the original answer survive a replay."""
    done = not request.accepted
    return SweepRecord(
        sweep_id=sweep_id,
        tenant_id=tenant,
        idempotency_key=request.idempotency_key,
        day=utc_day(now),
        effective_budget_usd=effective_budget(
            request.requested_budget_usd, settings.l9_sweep_max_daily_budget_usd
        ),
        candidates=request.accepted,
        rejected=request.rejected,
        state=COMPLETED if done else RUNNING,
        completed_at=now if done else None,
    )


async def _purge(store: SweepStore, settings: Any, now: datetime) -> None:
    """Delete expired sweeps. Retention is enforced by the one action that writes;
    the results action only reads (contract section 6)."""
    try:
        await store.purge_expired(now, timedelta(hours=settings.l9_sweep_result_ttl_hours))
    except Exception as exc:
        logger.warning("enrichment_sweep.purge_failed", error=str(exc))


def _start(store: SweepStore, record: SweepRecord, settings: Any) -> None:
    """Run `record` in the background; the task is held until it finishes."""
    runner = SweepRunner(
        store=store,
        assess=_wiring.assess or _gate_assess,
        converge=_wiring.converge or _canonical_converge,
        load_cartridge=_load_cartridge,
        settings=settings,
        clock=_wiring.clock,
    )
    task = asyncio.create_task(runner.run(record), name=f"enrichment-sweep:{record.sweep_id}")
    _background.add(task)
    _running_here.add(record.sweep_id)
    task.add_done_callback(_background.discard)
    task.add_done_callback(lambda _: _running_here.discard(record.sweep_id))


async def handle_enrichment_sweep_results(
    tenant: str,
    payload: dict[str, Any],
    packet: Any = None,
) -> dict[str, Any]:
    """One page of a sweep's results, its budget and its skips.

    Strictly read-only: it writes nothing, starts or resumes nothing, and the
    same request returns the same page. A sweep past its retention is answered
    `sweep_not_found` here and deleted later by `enrichment-sweep`.
    """
    settings = get_settings()
    _require_enabled(settings)
    sweep_id, after = _results_request(payload)
    limit = settings.l9_sweep_results_page_size
    record, rows, ledger = await _read(_store(), tenant, sweep_id, after, limit)
    ttl = timedelta(hours=settings.l9_sweep_result_ttl_hours)
    if record.completed_at is not None and record.completed_at < _wiring.clock() - ttl:
        raise SweepRefusedError("sweep_not_found", sweep_id)

    last = rows[-1].seq if rows else after
    finished = record.state == COMPLETED and len(rows) < limit
    effective = record.effective_budget_usd
    remaining = max(effective - ledger.spent_usd - ledger.reserved_usd, money(0))
    return {
        "sweep_id": sweep_id,
        "state": record.state,
        "results": [{"entity_id": r.entity_id, "response": r.response} for r in rows],
        "next_cursor": None if finished else str(last),
        "budget": {
            "effective_usd": _usd(effective),
            "spent_usd": _usd(ledger.spent_usd),
            "remaining_usd": _usd(remaining),
        },
        "skipped": list(record.skipped),
        "stats": dict(record.stats),
    }


def _results_request(payload: object) -> tuple[str, int]:
    """(sweep_id, position after which to read) from a results request."""
    if not isinstance(payload, dict) or payload.keys() - _RESULTS_KEYS:
        raise SweepRefusedError("invalid_request", "payload must be {sweep_id, cursor?}")
    sweep_id = payload.get("sweep_id")
    if not isinstance(sweep_id, str) or not sweep_id:
        raise SweepRefusedError("invalid_request", "sweep_id must be a nonempty string")
    return sweep_id, _cursor(payload.get("cursor"))


async def _read(
    store: SweepStore, tenant: str, sweep_id: str, after: int, limit: int
) -> tuple[SweepRecord, list[SweepResultRow], LedgerState]:
    """The sweep, one page of its results and its day's ledger, or a refusal."""
    try:
        record = await store.get_sweep(tenant, sweep_id)
        if record is None:
            raise SweepRefusedError("sweep_not_found", sweep_id)
        rows = await store.results_page(sweep_id, after, limit)
        ledger = await store.ledger(tenant, record.day)
    except SweepRefusedError:
        raise
    except Exception as exc:
        raise SweepRefusedError("sweep_store_unavailable", str(exc)) from exc
    return record, rows, ledger


def _cursor(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise SweepRefusedError("invalid_request", "cursor must be a next_cursor value")


__all__ = [
    "handle_enrichment_sweep",
    "handle_enrichment_sweep_results",
    "close_stale_sweeps",
    "init_sweep",
    "start_stale_sweep_poll",
    "stop_stale_sweep_poll",
    "wait_for_background_sweeps",
]


def _usd(amount: object) -> float:
    """A stored money amount as a JSON number; never a crash on a bad value."""
    try:
        return float(amount)  # type: ignore[arg-type]
    except (ValueError, TypeError, OverflowError):
        return 0.0
