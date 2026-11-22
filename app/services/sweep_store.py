"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: enrichment
layer: [services]
tags: [enrichment-sweep, persistence, budget]
owner: engine-team
status: active
--- /L9_META ---

Persistence for the enrichment sweep (docs/contracts/enrichment-sweep.md).

`SweepStore` is the narrow surface the sweep engine depends on. `PgSweepStore`
is its PostgreSQL implementation over the process engine `pg_store` owns
(tables in migration 004). Every method raises when the database is not
reachable; the engine reads any exception as "ledger unavailable" and enriches
nothing further (fail closed).

Money is `Decimal` end to end: a float ledger drifts, and a drifting cap is a
cap that can be passed.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from . import pg_store
from .pg_models import EnrichmentSweepRecord, EnrichmentSweepResult, SweepBudgetLedger

RUNNING = "running"
COMPLETED = "completed"
#: The skip reason every candidate an interrupted sweep never handled gets: the
#: contract's "Unexpected runner error" row (docs/contracts/enrichment-sweep.md,
#: section 5). An interrupted run is one; no new reason exists for it.
SWEEP_FAILED = "sweep_failed"


@dataclass(frozen=True)
class SweepRecord:
    """One sweep as persisted. `candidates` are the accepted, validated candidates."""

    sweep_id: str
    tenant_id: str
    idempotency_key: str
    day: date
    effective_budget_usd: Decimal
    candidates: tuple[dict[str, Any], ...]
    rejected: tuple[dict[str, Any], ...] = ()
    state: str = RUNNING
    skipped: tuple[dict[str, Any], ...] = ()
    stats: dict[str, Any] = field(default_factory=dict)
    completed_at: datetime | None = None


@dataclass(frozen=True)
class SweepResultRow:
    """One stored converge response body and its position in the sweep."""

    seq: int
    entity_id: str
    response: dict[str, Any]


@dataclass(frozen=True)
class LedgerState:
    """A tenant's sweep spend for one UTC day."""

    spent_usd: Decimal
    reserved_usd: Decimal


class SweepStore(Protocol):
    """What the sweep engine needs from persistence. Every method may raise."""

    async def create_sweep(self, record: SweepRecord) -> tuple[SweepRecord, bool]:
        """Insert `record`, or return the existing sweep for (tenant, key) and False."""

    async def get_sweep(self, tenant_id: str, sweep_id: str) -> SweepRecord | None:
        """The tenant's sweep, or None (another tenant's sweep is None too)."""

    async def reserve(self, tenant_id: str, day: date, amount: Decimal, cap: Decimal) -> bool:
        """Atomically reserve `amount` when spent + reserved + amount <= cap."""

    async def settle(self, tenant_id: str, day: date, reserved: Decimal, actual: Decimal) -> None:
        """Release a reservation and record the actual cost against the day."""

    async def ledger(self, tenant_id: str, day: date) -> LedgerState:
        """The tenant's spend and reservations for `day` (zero when no row exists)."""

    async def add_result(
        self, sweep_id: str, seq: int, entity_id: str, response: dict[str, Any], cost: Decimal
    ) -> None:
        """Store one converge response body, as returned."""

    async def save_progress(
        self,
        sweep_id: str,
        skipped: Sequence[dict[str, Any]],
        stats: dict[str, Any],
        *,
        completed_at: datetime | None = None,
    ) -> None:
        """Persist skips and counters; a `completed_at` also marks the sweep completed."""

    async def results_page(self, sweep_id: str, after_seq: int, limit: int) -> list[SweepResultRow]:
        """Up to `limit` results after `after_seq`, in order."""

    async def purge_expired(self, now: datetime, ttl: timedelta) -> int:
        """Delete sweeps completed more than `ttl` ago, with their results."""

    async def close_stale_sweeps(
        self, now: datetime, stale_after: timedelta, exclude: Collection[str] = ()
    ) -> list[str]:
        """Complete running sweeps whose worker is gone; returns their ids.

        A sweep is stale when nothing has touched it for `stale_after`: not its
        creation, not a stored result, not its tenant-day ledger row. Every
        candidate it never handled is skipped `sweep_failed`. The ledger is not
        touched: a reservation the dead worker held stays charged, never
        released into a second spend. `exclude` names sweeps this process runs.
        """


def interrupted_progress(
    record: SweepRecord, handled: Collection[str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The final skips and counters of a sweep interrupted mid-run.

    The candidates already skipped or enriched (`handled`) keep their outcome;
    every other one is skipped `sweep_failed`, counted under that reason.
    """
    done = {s["entity_id"] for s in record.skipped} | set(handled)
    skipped = list(record.skipped)
    stats = dict(record.stats)
    for candidate in record.candidates:
        entity_id = candidate["entity_id"]
        if entity_id in done:
            continue
        done.add(entity_id)
        skipped.append({"entity_id": entity_id, "reason": SWEEP_FAILED})
        stats[SWEEP_FAILED] = stats.get(SWEEP_FAILED, 0) + 1
    return skipped, stats


def _record(row: EnrichmentSweepRecord) -> SweepRecord:
    return SweepRecord(
        sweep_id=row.sweep_id,
        tenant_id=row.tenant_id,
        idempotency_key=row.idempotency_key,
        day=row.day,
        effective_budget_usd=Decimal(row.effective_budget_usd),
        candidates=tuple(row.candidates or ()),
        rejected=tuple(row.rejected or ()),
        state=row.state,
        skipped=tuple(row.skipped or ()),
        stats=dict(row.stats or {}),
        completed_at=row.completed_at,
    )


class PgSweepStore:
    """SweepStore over PostgreSQL, through `pg_store.get_session()`."""

    async def create_sweep(self, record: SweepRecord) -> tuple[SweepRecord, bool]:
        async with pg_store.get_session() as session:
            inserted = await session.execute(
                insert(EnrichmentSweepRecord)
                .values(
                    sweep_id=record.sweep_id,
                    tenant_id=record.tenant_id,
                    idempotency_key=record.idempotency_key,
                    state=record.state,
                    day=record.day,
                    effective_budget_usd=record.effective_budget_usd,
                    candidates=list(record.candidates),
                    rejected=list(record.rejected),
                    skipped=list(record.skipped),
                    stats=dict(record.stats),
                    completed_at=record.completed_at,
                )
                # Any conflict, not only (tenant, key): `sweep_id` is derived from
                # the same pair, so two concurrent deliveries of one request can
                # meet on the primary key first. Either way the row that exists is
                # this request's sweep, read back below.
                .on_conflict_do_nothing()
                .returning(EnrichmentSweepRecord.sweep_id)
            )
            if inserted.scalar_one_or_none() is not None:
                return record, True
            existing = await session.execute(
                select(EnrichmentSweepRecord).where(
                    EnrichmentSweepRecord.tenant_id == record.tenant_id,
                    EnrichmentSweepRecord.idempotency_key == record.idempotency_key,
                )
            )
            return _record(existing.scalar_one()), False

    async def get_sweep(self, tenant_id: str, sweep_id: str) -> SweepRecord | None:
        async with pg_store.get_session() as session:
            row = await session.get(EnrichmentSweepRecord, sweep_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return _record(row)

    async def reserve(self, tenant_id: str, day: date, amount: Decimal, cap: Decimal) -> bool:
        async with pg_store.get_session() as session:
            await session.execute(
                insert(SweepBudgetLedger)
                .values(tenant_id=tenant_id, day=day, spent_usd=0, reserved_usd=0)
                .on_conflict_do_nothing(index_elements=["tenant_id", "day"])
            )
            ledger = SweepBudgetLedger
            granted = await session.execute(
                update(ledger)
                .where(
                    ledger.tenant_id == tenant_id,
                    ledger.day == day,
                    ledger.spent_usd + ledger.reserved_usd + amount <= cap,
                )
                .values(reserved_usd=ledger.reserved_usd + amount, updated_at=func.now())
                .returning(ledger.tenant_id)
            )
            return granted.scalar_one_or_none() is not None

    async def settle(self, tenant_id: str, day: date, reserved: Decimal, actual: Decimal) -> None:
        ledger = SweepBudgetLedger
        async with pg_store.get_session() as session:
            await session.execute(
                update(ledger)
                .where(ledger.tenant_id == tenant_id, ledger.day == day)
                .values(
                    reserved_usd=ledger.reserved_usd - reserved,
                    spent_usd=ledger.spent_usd + actual,
                    updated_at=func.now(),
                )
            )

    async def ledger(self, tenant_id: str, day: date) -> LedgerState:
        async with pg_store.get_session() as session:
            row = await session.get(SweepBudgetLedger, (tenant_id, day))
            if row is None:
                return LedgerState(Decimal(0), Decimal(0))
            return LedgerState(Decimal(row.spent_usd), Decimal(row.reserved_usd))

    async def add_result(
        self, sweep_id: str, seq: int, entity_id: str, response: dict[str, Any], cost: Decimal
    ) -> None:
        async with pg_store.get_session() as session:
            await session.execute(
                insert(EnrichmentSweepResult)
                .values(
                    sweep_id=sweep_id,
                    seq=seq,
                    entity_id=entity_id,
                    response=response,
                    cost_usd=cost,
                )
                .on_conflict_do_nothing(constraint="uq_enrichment_sweep_results_entity")
            )

    async def save_progress(
        self,
        sweep_id: str,
        skipped: Sequence[dict[str, Any]],
        stats: dict[str, Any],
        *,
        completed_at: datetime | None = None,
    ) -> None:
        values: dict[str, Any] = {"skipped": list(skipped), "stats": dict(stats)}
        if completed_at is not None:
            values.update(state=COMPLETED, completed_at=completed_at)
        async with pg_store.get_session() as session:
            # Only a running sweep moves: once completed -- by its own worker or
            # closed as stale -- a late write from a worker cannot rewrite it.
            await session.execute(
                update(EnrichmentSweepRecord)
                .where(
                    EnrichmentSweepRecord.sweep_id == sweep_id,
                    EnrichmentSweepRecord.state == RUNNING,
                )
                .values(**values)
            )

    async def results_page(self, sweep_id: str, after_seq: int, limit: int) -> list[SweepResultRow]:
        async with pg_store.get_session() as session:
            rows = await session.execute(
                select(EnrichmentSweepResult)
                .where(
                    EnrichmentSweepResult.sweep_id == sweep_id,
                    EnrichmentSweepResult.seq > after_seq,
                )
                .order_by(EnrichmentSweepResult.seq)
                .limit(limit)
            )
            return [
                SweepResultRow(seq=r.seq, entity_id=r.entity_id, response=dict(r.response))
                for r in rows.scalars()
            ]

    async def purge_expired(self, now: datetime, ttl: timedelta) -> int:
        async with pg_store.get_session() as session:
            purged = await session.execute(
                delete(EnrichmentSweepRecord)
                .where(
                    EnrichmentSweepRecord.completed_at.is_not(None),
                    EnrichmentSweepRecord.completed_at < now - ttl,
                )
                .returning(EnrichmentSweepRecord.sweep_id)
            )
            return len(purged.scalars().all())

    async def close_stale_sweeps(
        self, now: datetime, stale_after: timedelta, exclude: Collection[str] = ()
    ) -> list[str]:
        sweep, result, ledger = EnrichmentSweepRecord, EnrichmentSweepResult, SweepBudgetLedger
        last_result = (
            select(func.max(result.created_at))
            .where(result.sweep_id == sweep.sweep_id)
            .scalar_subquery()
        )
        last_ledger = (
            select(ledger.updated_at)
            .where(ledger.tenant_id == sweep.tenant_id, ledger.day == sweep.day)
            .scalar_subquery()
        )
        # GREATEST ignores NULLs: a sweep with no result or no ledger row yet is
        # as live as its creation.
        last_touch = func.greatest(sweep.created_at, last_result, last_ledger)
        stale = select(sweep).where(sweep.state == RUNNING, last_touch < now - stale_after)
        if exclude:
            stale = stale.where(sweep.sweep_id.not_in(sorted(exclude)))
        closed: list[str] = []
        async with pg_store.get_session() as session:
            rows = (
                (await session.execute(stale.with_for_update(of=sweep, skip_locked=True)))
                .scalars()
                .all()
            )
            for row in rows:
                record = _record(row)
                handled = (
                    await session.execute(
                        select(result.entity_id).where(result.sweep_id == record.sweep_id)
                    )
                ).scalars()
                skipped, stats = interrupted_progress(record, set(handled))
                done = await session.execute(
                    update(sweep)
                    .where(sweep.sweep_id == record.sweep_id, sweep.state == RUNNING)
                    .values(skipped=skipped, stats=stats, state=COMPLETED, completed_at=now)
                    .returning(sweep.sweep_id)
                )
                closed.extend(done.scalars().all())
        return closed


__all__ = [
    "COMPLETED",
    "RUNNING",
    "SWEEP_FAILED",
    "LedgerState",
    "PgSweepStore",
    "SweepRecord",
    "SweepResultRow",
    "SweepStore",
    "interrupted_progress",
]
