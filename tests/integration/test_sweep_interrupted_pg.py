"""Q9 on PostgreSQL: a sweep whose worker died leaves `running` and never double-charges.

A real `SweepRunner` over `PgSweepStore` is killed mid-converge (its task is
cancelled while a reservation is held, as a process restart would leave it).
A later pass closes it: candidates already enriched keep their results, the
rest are skipped `sweep_failed`, and the ledger is untouched -- the held
reservation stays charged, so a later sweep of the same tenant and day can
never spend past the cap.

Needs a PostgreSQL the test may own: `EIE_TEST_DATABASE_URL`
(`postgresql+asyncpg://...`). Every test drops and recreates the schema.
Without it the module skips -- or fails under `EIE_REQUIRE_INTEGRATION=1`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from app.engines import sweep_handlers
from app.engines.enrichment_sweep import SweepRunner
from app.services import pg_store
from app.services.sweep_store import COMPLETED, RUNNING, PgSweepStore, SweepRecord
from tests.conftest import skip_or_fail_unavailable

pytestmark = pytest.mark.integration

TENANT = "synthco"
NODE = "Depot"
DB_URL = os.environ.get("EIE_TEST_DATABASE_URL", "")
# One converge reserves max_budget_tokens * rate / 1000 = $1.00.
SETTINGS = SimpleNamespace(max_budget_tokens=1000, token_rate_usd_per_1k=1.0)
STALE = sweep_handlers.SWEEP_STALE_AFTER


@pytest_asyncio.fixture
async def store() -> AsyncIterator[PgSweepStore]:
    if not DB_URL:
        skip_or_fail_unavailable("EIE_TEST_DATABASE_URL not set")
    pg_store.init_engine(DB_URL, pool_size=5, max_overflow=5)
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


def _record(key: str, n: int, budget: str = "3") -> SweepRecord:
    return SweepRecord(
        sweep_id=f"sw-{key}",
        tenant_id=TENANT,
        idempotency_key=key,
        day=datetime.now(UTC).date(),
        effective_budget_usd=Decimal(budget),
        candidates=tuple(
            {
                "entity_id": f"rec-{i}",
                "node_label": NODE,
                "entity": {"id": f"rec-{i}"},
                "field_confidences": {},
            }
            for i in range(n)
        ),
        state=RUNNING,
    )


async def _assess(tenant: str, payload: dict[str, Any], key: str) -> dict[str, Any]:
    return {
        "entities": [
            {
                "entity_id": e["entity_id"],
                "entity_health": {"critical_gaps": ["capacity"], "readiness_score": 0.3},
            }
            for e in payload["entities"]
        ]
    }


def _config() -> Any:
    raw = {"ontology": {"nodes": [{"label": NODE, "properties": [{"name": "capacity"}]}]}}
    return SimpleNamespace(version="1", source_sha256=None, raw_spec=raw)


class _Converge:
    """Answers the first `ok` converges, then hangs: the worker dies mid-converge."""

    def __init__(self, ok: int, tokens: int = 100) -> None:
        self.ok = ok
        self.tokens = tokens
        self.calls = 0
        self.hanging = asyncio.Event()

    async def __call__(self, tenant: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.calls > self.ok:
            self.hanging.set()
            await asyncio.Event().wait()  # never returns: killed here
        return {"state": "completed", "tokens_used": self.tokens, "entity_ref": body["entity_id"]}


def _runner(store: PgSweepStore, converge: Any) -> SweepRunner:
    return SweepRunner(
        store=store,
        assess=_assess,
        converge=converge,
        load_cartridge=lambda tenant: _config(),
        settings=SETTINGS,
    )


async def _kill_mid_converge(store: PgSweepStore, record: SweepRecord, ok: int) -> None:
    stored, created = await store.create_sweep(record)
    assert created
    converge = _Converge(ok)
    task = asyncio.create_task(_runner(store, converge).run(stored))
    await asyncio.wait_for(converge.hanging.wait(), timeout=10)
    task.cancel()  # the process dies holding the last reservation
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_sweep_killed_mid_converge_is_closed_and_never_double_charged(
    store: PgSweepStore,
) -> None:
    record = _record("k-1", n=4, budget="3")
    await _kill_mid_converge(store, record, ok=1)
    day = record.day
    before = await store.ledger(TENANT, day)
    # One converge settled at $0.10, the second still holds its $1.00.
    assert (before.spent_usd, before.reserved_usd) == (Decimal("0.1"), Decimal("1"))
    stuck = await store.get_sweep(TENANT, record.sweep_id)
    assert stuck is not None and stuck.state == RUNNING

    # Not yet stale: nothing is closed early.
    assert await store.close_stale_sweeps(datetime.now(UTC), STALE) == []

    later = datetime.now(UTC) + STALE + timedelta(minutes=1)
    assert await store.close_stale_sweeps(later, STALE) == [record.sweep_id]
    closed = await store.get_sweep(TENANT, record.sweep_id)
    assert closed is not None and closed.state == COMPLETED
    reasons = {s["entity_id"]: s["reason"] for s in closed.skipped}
    assert reasons == {"rec-1": "sweep_failed", "rec-2": "sweep_failed", "rec-3": "sweep_failed"}
    assert closed.stats["sweep_failed"] == 3
    assert [r.entity_id for r in await store.results_page(record.sweep_id, 0, 10)] == ["rec-0"]

    # The ledger is untouched: the held reservation stays charged.
    assert await store.ledger(TENANT, day) == before
    # A second close is a no-op.
    assert await store.close_stale_sweeps(later, STALE) == []

    # A later sweep of the same tenant and day cannot pass the cap ($3): its
    # converges cost their full $1.00, and the dead worker's $1.00 still counts.
    again = _record("k-2", n=5, budget="3")
    stored, _ = await store.create_sweep(again)
    later_converge = _Converge(ok=99, tokens=1000)
    await _runner(store, later_converge).run(stored)
    after = await store.ledger(TENANT, day)
    assert after.reserved_usd == before.reserved_usd  # only the dead worker's
    assert after.spent_usd + after.reserved_usd <= Decimal("3")
    assert later_converge.calls == 1  # $0.10 + $1.00 held + $1.00 = $2.10; a 2nd would be $3.10
    finished = await store.get_sweep(TENANT, again.sweep_id)
    assert finished is not None
    assert [s["reason"] for s in finished.skipped] == ["budget_exhausted"] * 4


@pytest.mark.asyncio
async def test_a_dead_worker_with_no_reservation_held_charges_nothing_more(
    store: PgSweepStore,
) -> None:
    record = _record("k-3", n=3)
    stored, _ = await store.create_sweep(record)
    # The worker died during readiness, before reserving anything.
    later = datetime.now(UTC) + STALE + timedelta(minutes=1)
    assert await store.close_stale_sweeps(later, STALE) == [stored.sweep_id]
    ledger = await store.ledger(TENANT, record.day)
    assert (ledger.spent_usd, ledger.reserved_usd) == (Decimal(0), Decimal(0))
    closed = await store.get_sweep(TENANT, stored.sweep_id)
    assert closed is not None
    assert [s["reason"] for s in closed.skipped] == ["sweep_failed"] * 3


@pytest.mark.asyncio
async def test_a_recent_ledger_write_keeps_a_sweep_alive(store: PgSweepStore) -> None:
    record = _record("k-4", n=2)
    stored, _ = await store.create_sweep(record)
    async with pg_store.get_session() as session:
        await session.execute(
            pg_store.text(
                "UPDATE enrichment_sweeps SET created_at = now() - interval '2 hours'"
                " WHERE sweep_id = :s"
            ).bindparams(s=stored.sweep_id)
        )
    # Created long ago, but its tenant-day ledger was just written: alive.
    assert await store.reserve(TENANT, record.day, Decimal("1"), Decimal("5"))
    assert await store.close_stale_sweeps(datetime.now(UTC), STALE) == []
    # Excluded sweeps are never closed, however old.
    old = datetime.now(UTC) + STALE + timedelta(minutes=1)
    assert await store.close_stale_sweeps(old, STALE, exclude={stored.sweep_id}) == []
    assert await store.close_stale_sweeps(old, STALE) == [stored.sweep_id]


@pytest.mark.asyncio
async def test_a_late_worker_neither_rewrites_nor_spends_after_its_sweep_closed(
    store: PgSweepStore,
) -> None:
    record = _record("k-5", n=3)
    stored, _ = await store.create_sweep(record)
    later = datetime.now(UTC) + STALE + timedelta(minutes=1)
    assert await store.close_stale_sweeps(later, STALE) == [stored.sweep_id]
    # The "dead" worker was only slow: it resumes with its stale record.
    converge = _Converge(ok=99)
    await _runner(store, converge).run(stored)
    assert converge.calls == 0  # stopped before reserving anything
    ledger = await store.ledger(TENANT, record.day)
    assert (ledger.spent_usd, ledger.reserved_usd) == (Decimal(0), Decimal(0))
    closed = await store.get_sweep(TENANT, stored.sweep_id)
    assert closed is not None and closed.state == COMPLETED
    assert [s["reason"] for s in closed.skipped] == ["sweep_failed"] * 3
