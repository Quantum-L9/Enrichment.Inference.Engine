"""Tier 2: an interrupted sweep ends in a state the sweep contract already allows.

Contract: docs/contracts/enrichment-sweep.md. A sweep is `running | completed`
(section 6.2); an unexpected runner error skips every candidate not yet handled
`sweep_failed` (section 5). A worker that died mid-sweep is that error, so the
closure introduces no new status, reason, field or action.

Pinned here without a database, against the statements `PgSweepStore` sends:

1. closure writes only `enrichment_sweeps`, only a row still `running`, and
   leaves `sweep_budget_ledger` alone: a reservation the dead worker held stays
   charged and is never released into a second spend;
2. staleness is keyed on existing timestamps -- the sweep's `created_at`, its
   results' `created_at`, its tenant-day ledger `updated_at` -- and every
   reservation and settlement refreshes that ledger timestamp;
3. once a sweep is completed, a late progress write cannot rewrite it.

PostgreSQL runs the restart mid-sweep in
tests/integration/test_sweep_interrupted_pg.py.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.services import pg_store
from app.services.pg_models import EnrichmentSweepRecord
from app.services.sweep_store import COMPLETED, RUNNING, SWEEP_FAILED, PgSweepStore

REPO = Path(__file__).resolve().parents[3]
DOC = REPO / "docs" / "contracts" / "enrichment-sweep.md"
TENANT = "synthco"
DAY = date(2026, 11, 22)
NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Rows:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def __iter__(self) -> Any:
        return iter(self._rows)


class _Session:
    def __init__(self, *answers: list[Any]) -> None:
        self.answers = [_Rows(a) for a in answers]
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Rows:
        self.statements.append(statement)
        return self.answers.pop(0) if self.answers else _Rows([])


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


def _row() -> EnrichmentSweepRecord:
    return EnrichmentSweepRecord(
        sweep_id="sw-1",
        tenant_id=TENANT,
        idempotency_key="k-1",
        state=RUNNING,
        day=DAY,
        effective_budget_usd=Decimal("3"),
        candidates=[{"entity_id": "rec-0"}, {"entity_id": "rec-1"}],
        rejected=[],
        skipped=[],
        stats={"enriched": 1},
        completed_at=None,
    )


def test_the_end_state_and_reason_are_ones_the_contract_already_names() -> None:
    doc = DOC.read_text(encoding="utf-8")
    assert re.search(r'"state": "running \| completed"', doc)
    assert (RUNNING, COMPLETED) == ("running", "completed")
    row = next(line for line in doc.splitlines() if "Unexpected runner error" in line)
    assert f"`{SWEEP_FAILED}`" in row and "not yet handled" in row


@pytest.mark.asyncio
async def test_closure_writes_only_the_running_sweep_and_never_the_ledger(
    sessions: list[_Session],
) -> None:
    tx = _Session([_row()], ["rec-0"], ["sw-1"])
    sessions.append(tx)
    closed = await PgSweepStore().close_stale_sweeps(NOW, timedelta(minutes=30), exclude={"sw-9"})
    assert closed == ["sw-1"]
    select_stale, handled, close = (_sql(s) for s in tx.statements)
    # Staleness: the latest of creation, results and the tenant-day ledger write.
    assert "greatest(enrichment_sweeps.created_at" in select_stale
    assert "max(enrichment_sweep_results.created_at)" in select_stale
    assert "sweep_budget_ledger.updated_at" in select_stale
    assert "FOR UPDATE OF enrichment_sweeps SKIP LOCKED" in select_stale
    assert "enrichment_sweeps.sweep_id NOT IN" in select_stale
    assert "FROM enrichment_sweep_results" in handled
    assert close.startswith("UPDATE enrichment_sweeps SET")
    assert "enrichment_sweeps.state = " in close
    params = tx.statements[2].compile(dialect=postgresql.dialect()).params
    assert params["state"] == COMPLETED and params["completed_at"] == NOW
    assert params["skipped"] == [{"entity_id": "rec-1", "reason": SWEEP_FAILED}]
    assert params["stats"] == {"enriched": 1, SWEEP_FAILED: 1}
    for statement in tx.statements:
        assert not _sql(statement).startswith("UPDATE sweep_budget_ledger")


@pytest.mark.asyncio
async def test_reserving_and_settling_refresh_the_ledger_timestamp(
    sessions: list[_Session],
) -> None:
    reserve_tx, settle_tx = _Session([], [TENANT]), _Session()
    sessions.extend([reserve_tx, settle_tx])

    class _Granted:
        def scalar_one_or_none(self) -> str:
            return TENANT

    async def granted(statement: Any) -> Any:
        reserve_tx.statements.append(statement)
        return _Granted()

    reserve_tx.execute = granted  # type: ignore[method-assign]
    store = PgSweepStore()
    assert await store.reserve(TENANT, DAY, Decimal("1"), Decimal("3"))
    await store.settle(TENANT, DAY, Decimal("1"), Decimal("0.1"))
    for statement in (reserve_tx.statements[1], settle_tx.statements[0]):
        sql = _sql(statement)
        assert sql.startswith("UPDATE sweep_budget_ledger SET")
        assert "updated_at=now()" in sql


@pytest.mark.asyncio
async def test_a_completed_sweep_cannot_be_rewritten_by_a_late_write(
    sessions: list[_Session],
) -> None:
    tx = _Session()
    sessions.append(tx)
    await PgSweepStore().save_progress("sw-1", [], {}, completed_at=NOW)
    sql = _sql(tx.statements[0])
    assert sql.startswith("UPDATE enrichment_sweeps SET")
    assert "enrichment_sweeps.sweep_id = " in sql and "enrichment_sweeps.state = " in sql
    assert RUNNING in tx.statements[0].compile(dialect=postgresql.dialect()).params.values()
