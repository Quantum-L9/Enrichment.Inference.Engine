"""A5 on PostgreSQL: the sweep ledger never exceeds its cap and a sweep key is created once.

Needs a PostgreSQL the test may own: `EIE_TEST_DATABASE_URL`
(`postgresql+asyncpg://...`). Every test drops and recreates the schema.
Without it the module skips -- or fails under `EIE_REQUIRE_INTEGRATION=1`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from app.services import pg_store
from app.services.sweep_store import RUNNING, PgSweepStore, SweepRecord
from tests.conftest import skip_or_fail_unavailable

pytestmark = pytest.mark.integration

TENANT = "synthco"
DAY = date(2026, 11, 22)
DB_URL = os.environ.get("EIE_TEST_DATABASE_URL", "")


@pytest_asyncio.fixture
async def store() -> AsyncIterator[PgSweepStore]:
    if not DB_URL:
        skip_or_fail_unavailable("EIE_TEST_DATABASE_URL not set")
    pg_store.init_engine(DB_URL, pool_size=10, max_overflow=10)
    engine = pg_store._engine
    assert engine is not None
    try:
        async with engine.begin() as conn:
            await conn.run_sync(pg_store.Base.metadata.drop_all)
            await conn.run_sync(pg_store.Base.metadata.create_all)
    except OSError as exc:
        await pg_store.close_engine()
        skip_or_fail_unavailable(f"PostgreSQL unreachable: {exc}")
    yield PgSweepStore()
    await pg_store.close_engine()


def _record(key: str) -> SweepRecord:
    return SweepRecord(
        sweep_id=f"sw-{key}",
        tenant_id=TENANT,
        idempotency_key=key,
        day=DAY,
        effective_budget_usd=Decimal("5"),
        candidates=({"entity_id": "rec-1", "node_label": "Depot", "entity": {"id": "rec-1"}},),
        state=RUNNING,
    )


@pytest.mark.asyncio
async def test_concurrent_reservations_never_exceed_the_cap(store: PgSweepStore) -> None:
    granted = await asyncio.gather(
        *(store.reserve(TENANT, DAY, Decimal("1"), Decimal("5")) for _ in range(20))
    )
    assert sum(granted) == 5
    ledger = await store.ledger(TENANT, DAY)
    assert (ledger.spent_usd, ledger.reserved_usd) == (Decimal(0), Decimal(5))


@pytest.mark.asyncio
async def test_settling_records_the_actual_cost_and_frees_the_rest(store: PgSweepStore) -> None:
    assert await store.reserve(TENANT, DAY, Decimal("1"), Decimal("2"))
    assert await store.reserve(TENANT, DAY, Decimal("1"), Decimal("2"))
    assert not await store.reserve(TENANT, DAY, Decimal("1"), Decimal("2"))
    await store.settle(TENANT, DAY, Decimal("1"), Decimal("0.25"))
    ledger = await store.ledger(TENANT, DAY)
    assert (ledger.spent_usd, ledger.reserved_usd) == (Decimal("0.25"), Decimal("1"))
    # The freed $0.75 is reservable again; nothing beyond the cap is.
    assert not await store.reserve(TENANT, DAY, Decimal("1"), Decimal("2"))
    assert await store.reserve(TENANT, DAY, Decimal("0.75"), Decimal("2"))


@pytest.mark.asyncio
async def test_an_unsettled_reservation_stays_charged_across_a_restart(
    store: PgSweepStore,
) -> None:
    """A process that died mid-converge leaves its reservation counted: never re-spent."""
    assert await store.reserve(TENANT, DAY, Decimal("3"), Decimal("5"))
    restarted = PgSweepStore()  # a new process reads the same ledger
    assert not await restarted.reserve(TENANT, DAY, Decimal("3"), Decimal("5"))
    assert await restarted.reserve(TENANT, DAY, Decimal("2"), Decimal("5"))
    ledger = await restarted.ledger(TENANT, DAY)
    assert ledger.spent_usd + ledger.reserved_usd == Decimal(5)


@pytest.mark.asyncio
async def test_ledgers_are_per_tenant_and_per_day(store: PgSweepStore) -> None:
    assert await store.reserve(TENANT, DAY, Decimal("5"), Decimal("5"))
    assert await store.reserve("othertenant", DAY, Decimal("5"), Decimal("5"))
    assert await store.reserve(TENANT, date(2026, 11, 23), Decimal("5"), Decimal("5"))
    assert not await store.reserve(TENANT, DAY, Decimal("0.01"), Decimal("5"))


@pytest.mark.asyncio
async def test_concurrent_sweeps_with_one_key_create_one_sweep(store: PgSweepStore) -> None:
    outcomes = await asyncio.gather(*(store.create_sweep(_record("k-1")) for _ in range(8)))
    assert sum(created for _, created in outcomes) == 1
    assert {record.sweep_id for record, _ in outcomes} == {"sw-k-1"}


@pytest.mark.asyncio
async def test_results_and_progress_survive_a_restart(store: PgSweepStore) -> None:
    record, _ = await store.create_sweep(_record("k-2"))
    await store.add_result(record.sweep_id, 1, "rec-1", {"state": "completed"}, Decimal("0.1"))
    await store.add_result(record.sweep_id, 1, "rec-1", {"state": "completed"}, Decimal("0.1"))
    await store.save_progress(
        record.sweep_id,
        [{"entity_id": "rec-2", "reason": "budget_exhausted"}],
        {"enriched": 1, "budget_exhausted": 1},
        completed_at=datetime(2026, 11, 22, 12, tzinfo=UTC),
    )
    restarted = PgSweepStore()
    stored = await restarted.get_sweep(TENANT, record.sweep_id)
    assert stored is not None and stored.state == "completed"
    assert stored.skipped == ({"entity_id": "rec-2", "reason": "budget_exhausted"},)
    rows = await restarted.results_page(record.sweep_id, 0, 10)
    assert [r.entity_id for r in rows] == ["rec-1"]
    assert await restarted.get_sweep("othertenant", record.sweep_id) is None
