"""A late write of an uncertain send never lands after a newer version (#419 review).

Contract: docs/contracts/data/graph_sync_intents.md *Ordering* and G4. Scenario:
version B is sent, its outcome is never learned (retries exhausted), and CEG
applies it late. The newer version A of the same subject is held until B's
max-apply window has passed, so B's late write lands first and A, sent after,
is the last write: the graph converges to A. A's field is not echo-suppressed,
because B -- sent, never acknowledged -- leaves it unsettled.

Needs a PostgreSQL the test may own: ``EIE_TEST_DATABASE_URL``. Without it the
module skips -- or fails under ``EIE_REQUIRE_INTEGRATION=1``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.engines import sync_field_set as fs
from app.services import pg_store
from tests.conftest import skip_or_fail_unavailable

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

DB_URL = os.environ.get("EIE_TEST_DATABASE_URL", "")
TENANT, ENTITY, ENDPOINT = "synthco", "st-1", "stations"
WINDOW = pg_store.GRAPH_MAX_APPLY_SECONDS


@pytest_asyncio.fixture
async def db() -> AsyncIterator[None]:
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
    yield
    await pg_store.close_engine()


def _t0() -> datetime:
    """After every row's server-default ``next_attempt_at``."""
    return datetime.now(UTC) + timedelta(seconds=5)


async def _intent(key: str, fields: dict[str, Any], *, max_attempts: int = 2) -> None:
    async with pg_store.get_session() as session:
        session.add(
            pg_store.GraphSyncIntent(
                tenant_id=TENANT,
                effect_key=key,
                entity_id=ENTITY,
                endpoint=ENDPOINT,
                origin="converge",
                fields=fields,
                max_attempts=max_attempts,
            )
        )


async def _claim(at: datetime) -> list[str]:
    claimed = await pg_store.claim_graph_intents(owner="w", lease_seconds=60, now=at)
    return [r.effect_key for r in claimed]


async def _claim_one(key: str, at: datetime) -> pg_store.GraphIntentRecord:
    claimed = await pg_store.claim_graph_intents(owner="w", lease_seconds=60, now=at)
    assert [r.effect_key for r in claimed] == [key]
    return claimed[0]


async def _settle(record: pg_store.GraphIntentRecord, outcome: str, at: datetime) -> str | None:
    return await pg_store.settle_graph_intent(
        record.id, owner=record.lease_owner, outcome=outcome, error="gate_timeout", now=at
    )


async def _due_now(key: str) -> None:
    async with pg_store.get_session() as session:  # the backoff has elapsed
        await session.execute(
            text(
                "UPDATE graph_sync_intents SET next_attempt_at = '2000-01-01T00:00:00Z'"
                " WHERE effect_key = :key"
            ),
            {"key": key},
        )


async def test_an_uncertain_send_applied_late_is_corrected_by_the_newer_version(
    db: None,
) -> None:
    graph: dict[str, Any] = {}  # what CEG holds, written in apply order
    b_fields = {"band": "B1", "band_provenance": "observed"}
    a_fields = {"band": "B2", "band_provenance": "enriched"}
    t0 = _t0()

    await _intent("b", b_fields)
    b = await _claim_one("b", t0)
    assert await _settle(b, "retry", t0 + timedelta(seconds=21)) == "pending"  # timed out
    await _due_now("b")
    b = await _claim_one("b", t0 + timedelta(seconds=30))
    last = t0 + timedelta(seconds=51)
    assert await _settle(b, "retry", last) == "failed_terminal"  # outcome never learned

    await _intent("a", a_fields)
    # A converge re-selecting B's value is not echo-suppressed: B is unsettled.
    assert await pg_store.graph_sync_settled_fields(TENANT, ENTITY, ENDPOINT) == {}
    assert fs.apply_echo_guard(a_fields, {"band": "enriched"}, {}) == a_fields

    # Inside B's max-apply window A is held: B may still be applied.
    assert await _claim(last + timedelta(seconds=1)) == []
    assert await _claim(last + timedelta(seconds=WINDOW - 1)) == []
    graph.update(b_fields)  # CEG applies B late, inside its window

    a = await _claim_one("a", last + timedelta(seconds=WINDOW + 1))
    graph.update(a.fields)  # A is the last write
    await _settle(a, "acknowledged", last + timedelta(seconds=WINDOW + 2))

    assert graph == a_fields
    settled = await pg_store.graph_sync_settled_fields(TENANT, ENTITY, ENDPOINT)
    assert settled == {}  # B stays unacknowledged: a later B2 is still resent


async def test_an_acknowledged_single_attempt_does_not_hold_the_newer_version(
    db: None,
) -> None:
    t0 = _t0()
    await _intent("b", {"band": "B1"})
    b = await _claim_one("b", t0)
    await _settle(b, "acknowledged", t0 + timedelta(seconds=1))
    await _intent("a", {"band": "B2"})
    assert await _claim(t0 + timedelta(seconds=2)) == ["a"]


async def test_a_definitive_refusal_does_not_hold_the_newer_version(db: None) -> None:
    t0 = _t0()
    await _intent("b", {"band": "B1"})
    b = await _claim_one("b", t0)
    assert await _settle(b, "terminal", t0 + timedelta(seconds=1)) == "failed_terminal"
    await _intent("a", {"band": "B2"})
    assert await _claim(t0 + timedelta(seconds=2)) == ["a"]


async def test_a_retried_then_acknowledged_version_holds_for_its_window(db: None) -> None:
    """Its first, timed-out request may still be in flight at CEG."""
    t0 = _t0()
    await _intent("b", {"band": "B1"}, max_attempts=3)
    b = await _claim_one("b", t0)
    await _settle(b, "retry", t0 + timedelta(seconds=21))
    await _due_now("b")
    b = await _claim_one("b", t0 + timedelta(seconds=30))
    acked = t0 + timedelta(seconds=31)
    await _settle(b, "acknowledged", acked)
    await _intent("a", {"band": "B2"})
    assert await _claim(acked + timedelta(seconds=1)) == []
    assert await _claim(acked + timedelta(seconds=WINDOW + 1)) == ["a"]


async def test_the_window_holds_only_the_same_subject(db: None) -> None:
    t0 = _t0()
    await _intent("b", {"band": "B1"}, max_attempts=1)
    b = await _claim_one("b", t0)
    assert await _settle(b, "retry", t0 + timedelta(seconds=21)) == "failed_terminal"
    async with pg_store.get_session() as session:
        session.add(
            pg_store.GraphSyncIntent(
                tenant_id=TENANT,
                effect_key="other",
                entity_id="st-2",
                endpoint=ENDPOINT,
                origin="converge",
                fields={"band": "B9"},
            )
        )
    assert await _claim(t0 + timedelta(seconds=22)) == ["other"]
