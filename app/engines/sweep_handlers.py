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
from ..services.sweep_store import COMPLETED, PgSweepStore, SweepRecord, SweepStore
from .domain_yaml_reader import DomainEnrichmentConfig, DomainYamlReader
from .enrichment_sweep import (
    ASSESS_ACTION,
    AssessFn,
    ConvergeFn,
    SweepRefusedError,
    SweepRunner,
    effective_budget,
    money,
    sweep_id_for,
    utc_day,
    validate_request,
)

logger = structlog.get_logger("sweep_handlers")

_RESULTS_KEYS = frozenset({"sweep_id", "cursor"})


@dataclass
class _Wiring:
    reader: DomainYamlReader | None = None
    store: SweepStore | None = None
    assess: AssessFn | None = None
    converge: ConvergeFn | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


_wiring = _Wiring()
_background: set[asyncio.Task[None]] = set()


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
    if not request.accepted:
        return {"sweep_id": sweep_id, "accepted": [], "rejected": list(request.rejected)}

    now = _wiring.clock()
    record = SweepRecord(
        sweep_id=sweep_id,
        tenant_id=tenant,
        idempotency_key=request.idempotency_key,
        day=utc_day(now),
        effective_budget_usd=effective_budget(
            request.requested_budget_usd, settings.l9_sweep_max_daily_budget_usd
        ),
        candidates=request.accepted,
        rejected=request.rejected,
    )
    store = _store()
    try:
        stored, created = await store.create_sweep(record)
    except Exception as exc:
        logger.error("enrichment_sweep.store_unavailable", tenant=tenant, error=str(exc))
        raise SweepRefusedError("sweep_store_unavailable", str(exc)) from exc
    if not created:
        # Same (tenant, idempotency_key): the sweep already exists; nothing restarts.
        return {
            "sweep_id": stored.sweep_id,
            "accepted": [c["entity_id"] for c in stored.candidates],
            "rejected": list(stored.rejected),
        }

    runner = SweepRunner(
        store=store,
        assess=_wiring.assess or _gate_assess,
        converge=_wiring.converge or _canonical_converge,
        load_cartridge=lambda t: _load_cartridge(t),
        settings=settings,
        clock=_wiring.clock,
    )
    task = asyncio.create_task(runner.run(stored), name=f"enrichment-sweep:{sweep_id}")
    _background.add(task)
    task.add_done_callback(_background.discard)
    logger.info(
        "enrichment_sweep.accepted",
        tenant=tenant,
        sweep_id=sweep_id,
        accepted=len(accepted_ids),
        rejected=len(request.rejected),
        effective_budget_usd=str(record.effective_budget_usd),
    )
    return {"sweep_id": sweep_id, "accepted": accepted_ids, "rejected": list(request.rejected)}


async def handle_enrichment_sweep_results(
    tenant: str,
    payload: dict[str, Any],
    packet: Any = None,
) -> dict[str, Any]:
    """One page of a sweep's results, its budget and its skips."""
    settings = get_settings()
    _require_enabled(settings)
    if not isinstance(payload, dict) or payload.keys() - _RESULTS_KEYS:
        raise SweepRefusedError("invalid_request", "payload must be {sweep_id, cursor?}")
    sweep_id = payload.get("sweep_id")
    if not isinstance(sweep_id, str) or not sweep_id:
        raise SweepRefusedError("invalid_request", "sweep_id must be a nonempty string")
    after = _cursor(payload.get("cursor"))

    store = _store()
    now = _wiring.clock()
    try:
        await store.purge_expired(now, timedelta(hours=settings.l9_sweep_result_ttl_hours))
    except Exception as exc:
        logger.warning("enrichment_sweep.purge_failed", error=str(exc))
    try:
        record = await store.get_sweep(tenant, sweep_id)
        if record is None:
            raise SweepRefusedError("sweep_not_found", sweep_id)
        limit = settings.l9_sweep_results_page_size
        rows = await store.results_page(sweep_id, after, limit)
        ledger = await store.ledger(tenant, record.day)
    except SweepRefusedError:
        raise
    except Exception as exc:
        raise SweepRefusedError("sweep_store_unavailable", str(exc)) from exc

    last = rows[-1].seq if rows else after
    finished = record.state == COMPLETED and len(rows) < limit
    if finished:
        try:
            await store.mark_fetched(sweep_id, now)
        except Exception as exc:
            logger.warning("enrichment_sweep.fetch_not_recorded", error=str(exc))
    effective = record.effective_budget_usd
    remaining = max(effective - ledger.spent_usd - ledger.reserved_usd, money(0))
    return {
        "sweep_id": sweep_id,
        "state": record.state,
        "results": [{"entity_id": r.entity_id, "response": r.response} for r in rows],
        "next_cursor": None if finished else str(last),
        "budget": {
            "effective_usd": float(effective),
            "spent_usd": float(ledger.spent_usd),
            "remaining_usd": float(remaining),
        },
        "skipped": list(record.skipped),
        "stats": dict(record.stats),
    }


def _cursor(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise SweepRefusedError("invalid_request", "cursor must be a next_cursor value")


__all__ = [
    "handle_enrichment_sweep",
    "handle_enrichment_sweep_results",
    "init_sweep",
    "wait_for_background_sweeps",
]
