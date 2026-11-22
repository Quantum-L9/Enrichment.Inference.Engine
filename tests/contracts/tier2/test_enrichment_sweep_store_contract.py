"""Tier 2: the sweep store keeps one sweep per key and never reserves past the cap.

Contract: docs/contracts/enrichment-sweep.md -- a replayed request (same tenant
and idempotency key) gets the original sweep and no new work or reservation;
a tenant's spend for one UTC day never exceeds the effective budget.

Pinned here without a database, against the statements `PgSweepStore` sends:

1. one sweep per (tenant, key): the insert ignores *any* conflict -- the
   (tenant, key) constraint or the primary key `sweep_id`, which is derived
   from the same pair -- and the existing sweep is read back by (tenant, key);
2. reservations: one ledger row per (tenant, day), and a reservation is a single
   conditional UPDATE granted only while spent + reserved + amount <= cap.

The same properties under real concurrency are run on PostgreSQL in
tests/integration/test_sweep_store_pg.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.engines.enrichment_sweep import sweep_id_for
from app.services import pg_store
from app.services.pg_models import EnrichmentSweepRecord, SweepBudgetLedger
from app.services.sweep_store import RUNNING, PgSweepStore, SweepRecord

TENANT = "synthco"
DAY = date(2026, 11, 22)


class _Result:
    def __init__(self, value: Any = None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar_one(self) -> Any:
        assert self._value is not None
        return self._value


class _Session:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return self.answers.pop(0) if self.answers else _Result()


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> list[_Session]:
    queue: list[_Session] = []

    @asynccontextmanager
    async def get_session() -> AsyncIterator[_Session]:
        yield queue.pop(0)

    monkeypatch.setattr(pg_store, "get_session", get_session)
    return queue


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _record(key: str = "k-1") -> SweepRecord:
    return SweepRecord(
        sweep_id=sweep_id_for(TENANT, key),
        tenant_id=TENANT,
        idempotency_key=key,
        day=DAY,
        effective_budget_usd=Decimal("5"),
        candidates=({"entity_id": "rec-1"},),
        state=RUNNING,
    )


def test_the_sweep_id_is_a_function_of_tenant_and_key_only() -> None:
    """Why a primary-key conflict means the same request: the id is derived from the pair."""
    assert sweep_id_for(TENANT, "k-1") == sweep_id_for(TENANT, "k-1")
    assert sweep_id_for(TENANT, "k-1") != sweep_id_for(TENANT, "k-2")
    assert sweep_id_for(TENANT, "k-1") != sweep_id_for("othertenant", "k-1")


def test_one_sweep_per_tenant_and_key_is_declared() -> None:
    uniques = {
        c.name: tuple(col.name for col in c.columns)
        for c in EnrichmentSweepRecord.__table__.constraints
        if c.name == "uq_enrichment_sweeps_tenant_key"
    }
    assert uniques == {"uq_enrichment_sweeps_tenant_key": ("tenant_id", "idempotency_key")}
    assert [c.name for c in EnrichmentSweepRecord.__table__.primary_key.columns] == ["sweep_id"]


@pytest.mark.asyncio
async def test_a_first_delivery_creates_the_sweep(sessions: list[_Session]) -> None:
    tx = _Session(_Result("sweep"))
    sessions.append(tx)
    record = _record()
    assert await PgSweepStore().create_sweep(record) == (record, True)
    (insert,) = tx.statements
    sql = _sql(insert)
    # Any conflict -- not only one named constraint -- is a duplicate delivery.
    assert sql.endswith("ON CONFLICT DO NOTHING RETURNING enrichment_sweeps.sweep_id")
    assert "ON CONSTRAINT" not in sql


@pytest.mark.asyncio
async def test_a_concurrent_delivery_reads_the_existing_sweep_back(
    sessions: list[_Session],
) -> None:
    existing = EnrichmentSweepRecord(
        sweep_id=sweep_id_for(TENANT, "k-1"),
        tenant_id=TENANT,
        idempotency_key="k-1",
        state="completed",
        day=DAY,
        effective_budget_usd=Decimal("5"),
        candidates=[{"entity_id": "rec-1"}],
        rejected=[],
        skipped=[],
        stats={"enriched": 1},
        completed_at=None,
    )
    tx = _Session(_Result(None), _Result(existing))
    sessions.append(tx)
    stored, created = await PgSweepStore().create_sweep(_record())
    assert created is False
    assert (stored.sweep_id, stored.state, stored.stats) == (
        existing.sweep_id,
        "completed",
        {"enriched": 1},
    )
    lookup = _sql(tx.statements[1])
    assert "enrichment_sweeps.tenant_id = " in lookup
    assert "enrichment_sweeps.idempotency_key = " in lookup


def test_the_ledger_is_one_row_per_tenant_and_day() -> None:
    assert [c.name for c in SweepBudgetLedger.__table__.primary_key.columns] == [
        "tenant_id",
        "day",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("granted", [True, False])
async def test_a_reservation_is_one_conditional_update_bounded_by_the_cap(
    sessions: list[_Session], granted: bool
) -> None:
    tx = _Session(_Result(), _Result(TENANT if granted else None))
    sessions.append(tx)
    assert await PgSweepStore().reserve(TENANT, DAY, Decimal("1"), Decimal("5")) is granted
    ensure, reserve = tx.statements
    assert "ON CONFLICT (tenant_id, day) DO NOTHING" in _sql(ensure)
    sql = _sql(reserve)
    assert sql.startswith("UPDATE sweep_budget_ledger SET reserved_usd=")
    assert "sweep_budget_ledger.tenant_id = " in sql and "sweep_budget_ledger.day = " in sql
    # The cap check is in the UPDATE itself: atomic under concurrent reservations.
    assert (
        "sweep_budget_ledger.spent_usd + sweep_budget_ledger.reserved_usd + " in sql
        and " <= " in sql
    )
    params = reserve.compile(dialect=postgresql.dialect()).params
    assert Decimal("5") in params.values()
