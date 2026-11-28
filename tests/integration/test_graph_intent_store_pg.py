"""A2 on PostgreSQL: graph intents are atomic with results, leased, ordered, bounded.

Needs a PostgreSQL the test may own: `EIE_TEST_DATABASE_URL`
(`postgresql+asyncpg://...`). Every test drops and recreates the schema.
Without it the module skips -- or fails under `EIE_REQUIRE_INTEGRATION=1`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.engines.packet_router import GraphSyncOutcome, GraphSyncOutcomeKind, GraphSyncPin
from app.services import pg_store
from app.services.side_effect_coordinator import SideEffectCoordinator
from tests.conftest import skip_or_fail_unavailable

pytestmark = pytest.mark.integration

TENANT = "synthco"
DB_URL = os.environ.get("EIE_TEST_DATABASE_URL", "")


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


def _spec(key: str = "op-1", **overrides: Any) -> pg_store.GraphIntentSpec:
    values: dict[str, Any] = {
        "tenant_id": TENANT,
        "effect_key": key,
        "entity_id": "d-1",
        "fields": {"capacity": 2.5},
        "domain": TENANT,
        "node_label": "Depot",
        "endpoint": "depots",
        "contract_digest": "c" * 64,
    }
    values.update(overrides)
    return pg_store.GraphIntentSpec(**values)


async def _save(spec: pg_store.GraphIntentSpec | None, **kwargs: Any) -> Any:
    with pg_store.graph_intent_scope(spec):
        return await pg_store.save_enrichment_result(
            tenant_id=TENANT,
            entity_id=kwargs.pop("entity_id", "d-1"),
            object_type="Depot",
            fields={"capacity": 2.5},
            confidence=0.9,
            uncertainty_score=0.1,
            tokens_used=1,
            processing_time_ms=1,
            pass_count=1,
            **kwargs,
        )


async def _count(table: str) -> int:
    async with pg_store.get_session() as session:
        query = {
            "enrichment_results": "SELECT count(*) FROM enrichment_results",
            "graph_sync_intents": "SELECT count(*) FROM graph_sync_intents",
        }[table]
        return int((await session.execute(text(query))).scalar_one())


# ── Atomic with the result ──────────────────────────────────────────────────


async def test_intent_is_written_in_the_results_transaction(db: None) -> None:
    record = await _save(_spec())
    intent = await pg_store.get_graph_intent(TENANT, "op-1")
    assert intent is not None
    assert intent.enrichment_result_id == record.id
    assert intent.state == "pending" and intent.attempts == 0
    assert (intent.endpoint, intent.contract_digest) == ("depots", "c" * 64)


async def test_a_duplicate_commit_rolls_back_whole(db: None) -> None:
    await _save(_spec())
    with pytest.raises(pg_store.GraphIntentDuplicateError):
        await _save(_spec())
    assert await _count("enrichment_results") == 1
    assert await _count("graph_sync_intents") == 1


async def test_two_workers_committing_one_operation_leave_one_result(db: None) -> None:
    """P20/P25: exactly one durable obligation (and result) survives a race."""
    outcomes = await asyncio.gather(_save(_spec()), _save(_spec()), return_exceptions=True)
    errors = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(errors) == 1 and isinstance(errors[0], pg_store.GraphIntentDuplicateError)
    assert await _count("enrichment_results") == 1
    assert await _count("graph_sync_intents") == 1


async def test_idempotent_hit_records_a_missing_intent_once(db: None) -> None:
    await _save(None, idempotency_key="caller-1")  # a result older than intents
    await _save(_spec(), idempotency_key="caller-1")
    assert await _count("enrichment_results") == 1
    assert await _count("graph_sync_intents") == 1
    with pytest.raises(pg_store.GraphIntentDuplicateError):
        await _save(_spec(), idempotency_key="caller-1")


async def test_concurrent_saves_with_one_idempotency_key_return_one_record(db: None) -> None:
    """The idempotency pre-check races; the index answers the replay, not an error."""
    a, b = await asyncio.gather(
        _save(None, idempotency_key="caller-1"), _save(None, idempotency_key="caller-1")
    )
    assert a.id == b.id
    assert await _count("enrichment_results") == 1


async def test_an_intent_for_another_tenant_is_refused(db: None) -> None:
    with pytest.raises(ValueError, match="graph intent tenant"):
        await _save(_spec(tenant_id="other"))
    assert await _count("enrichment_results") == 0


async def test_typed_list_of_maps_survives_the_store_as_maps(db: None) -> None:
    """P18 persistence half: a pinned list<map> replays as a list of mappings."""
    items = [{"code": "A1", "form": "flake"}, {"code": "B2"}]
    await _save(_spec(fields={"handled_items": items}))
    intent = await pg_store.get_graph_intent(TENANT, "op-1")
    assert intent is not None
    assert intent.fields["handled_items"] == items


# ── Leases ──────────────────────────────────────────────────────────────────


async def test_two_workers_never_lease_the_same_intent(db: None) -> None:
    for n in range(6):
        await _save(_spec(f"op-{n}", entity_id=f"d-{n}"), entity_id=f"d-{n}")
    a, b = await asyncio.gather(
        pg_store.claim_graph_intents(owner="w1", lease_seconds=30, limit=6),
        pg_store.claim_graph_intents(owner="w2", lease_seconds=30, limit=6),
    )
    keys_a, keys_b = {r.effect_key for r in a}, {r.effect_key for r in b}
    assert not keys_a & keys_b
    assert len(keys_a | keys_b) == 6


async def test_an_expired_lease_is_reclaimed_and_the_old_holder_settles_nothing(db: None) -> None:
    await _save(_spec())
    t0 = datetime.now(UTC)
    (first,) = await pg_store.claim_graph_intents(owner="w1", lease_seconds=10, now=t0)
    assert await pg_store.claim_graph_intents(owner="w2", lease_seconds=10, now=t0) == []
    later = t0 + timedelta(seconds=11)
    (second,) = await pg_store.claim_graph_intents(owner="w2", lease_seconds=10, now=later)
    assert second.attempts == 2
    assert first.lease_owner and first.lease_owner.startswith("w1#")
    assert (
        await pg_store.settle_graph_intent(
            first.id, owner=first.lease_owner, outcome="acknowledged"
        )
        is None
    )
    state = await pg_store.settle_graph_intent(
        second.id, owner=second.lease_owner, outcome="acknowledged"
    )
    assert state == "acknowledged"


async def test_retry_backs_off_then_exhausts_to_terminal(db: None) -> None:
    await _save(_spec(max_attempts=2))
    t0 = datetime.now(UTC)
    (claim,) = await pg_store.claim_graph_intents(owner="w1", lease_seconds=30, now=t0)
    state = await pg_store.settle_graph_intent(
        claim.id,
        owner=claim.lease_owner,
        outcome="retry",
        error="unacknowledged",
        retry_base_seconds=5,
        now=t0,
    )
    assert state == "pending"
    assert await pg_store.claim_graph_intents(owner="w1", lease_seconds=30, now=t0) == []
    (claim,) = await pg_store.claim_graph_intents(
        owner="w1", lease_seconds=30, now=t0 + timedelta(seconds=6)
    )
    state = await pg_store.settle_graph_intent(
        claim.id, owner=claim.lease_owner, outcome="retry", error="unacknowledged", now=t0
    )
    assert state == "failed_terminal"
    intent = await pg_store.get_graph_intent(TENANT, "op-1")
    assert intent is not None and intent.last_error == "retry_exhausted:unacknowledged"
    counts = await pg_store.graph_intent_status_counts(TENANT)
    assert counts == {"failed_terminal": 1}


async def test_a_dead_holder_on_its_last_attempt_ends_terminal(db: None) -> None:
    """P21: shutdown mid-attempt with the budget spent -> terminal, never 'synced'."""
    await _save(_spec(max_attempts=1))
    t0 = datetime.now(UTC)
    await pg_store.claim_graph_intents(owner="w1", lease_seconds=5, now=t0)
    assert (
        await pg_store.claim_graph_intents(
            owner="w2", lease_seconds=5, now=t0 + timedelta(seconds=6)
        )
        == []
    )
    intent = await pg_store.get_graph_intent(TENANT, "op-1")
    assert intent is not None
    assert (intent.state, intent.last_error) == (
        "failed_terminal",
        "retry_exhausted:lease_expired",
    )


# ── Version order ───────────────────────────────────────────────────────────


async def test_a_newer_version_waits_for_the_older_one(db: None) -> None:
    await _save(_spec("v1", fields={"capacity": 1.0}))
    await _save(_spec("v2", fields={"capacity": 2.0}))
    (older,) = await pg_store.claim_graph_intents(owner="w1", lease_seconds=30)
    assert older.effect_key == "v1"
    assert await pg_store.claim_graph_intents(owner="w2", lease_seconds=30) == []
    assert (
        await pg_store.claim_graph_intents(
            owner="w2", lease_seconds=30, tenant_id=TENANT, effect_key="v2"
        )
        == []
    )
    await pg_store.settle_graph_intent(older.id, owner=older.lease_owner, outcome="acknowledged")
    (newer,) = await pg_store.claim_graph_intents(owner="w2", lease_seconds=30)
    assert newer.effect_key == "v2" and newer.fields == {"capacity": 2.0}


async def test_an_older_version_never_lands_after_a_newer_sent_one(db: None) -> None:
    await _save(_spec("v1", fields={"capacity": 1.0}))
    await _save(_spec("v2", fields={"capacity": 2.0}))
    async with pg_store.get_session() as session:
        await session.execute(
            text(
                "UPDATE graph_sync_intents SET state = 'acknowledged', attempts = 1"
                " WHERE effect_key = 'v2'"
            )
        )
    assert await pg_store.claim_graph_intents(owner="w1", lease_seconds=30) == []
    older = await pg_store.get_graph_intent(TENANT, "v1")
    assert older is not None
    assert (older.state, older.last_error) == ("superseded", "superseded_by_newer_version")


async def test_a_newer_version_back_to_pending_still_supersedes_the_older(db: None) -> None:
    """Sent once, ack lost, back to `pending`: the older version must not land after it."""
    await _save(_spec("v1", fields={"capacity": 1.0}))
    await _save(_spec("v2", fields={"capacity": 2.0}))
    async with pg_store.get_session() as session:
        await session.execute(
            text("UPDATE graph_sync_intents SET attempts = 1 WHERE effect_key = 'v2'")
        )
    claimed = await pg_store.claim_graph_intents(owner="w1", lease_seconds=30)
    assert [r.effect_key for r in claimed] == ["v2"]
    older = await pg_store.get_graph_intent(TENANT, "v1")
    assert older is not None and older.state == "superseded"


async def test_a_claim_waits_for_an_uncommitted_claim_of_the_same_subject(db: None) -> None:
    """Worker B has leased the newer version but not committed; A must not send the older."""
    await _save(_spec("v1", fields={"capacity": 1.0}))
    await _save(_spec("v2", fields={"capacity": 2.0}))
    engine = pg_store._engine
    assert engine is not None
    subject = f"{TENANT}|d-1|depots"
    async with engine.connect() as worker_b:
        tx = await worker_b.begin()
        await worker_b.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:s, 0))"), {"s": subject}
        )
        await worker_b.execute(
            text(
                "UPDATE graph_sync_intents SET state = 'dispatching', attempts = 1,"
                " lease_owner = 'b', lease_expires_at = now() + interval '1 minute'"
                " WHERE effect_key = 'v2'"
            )
        )
        # v1 is the older outstanding row, so A's candidate is v1; its claim
        # must wait on B's subject lock rather than miss B's uncommitted lease.
        async with pg_store.get_session() as session:
            await session.execute(
                text("UPDATE graph_sync_intents SET state = 'pending' WHERE effect_key = 'v1'")
            )
        worker_a = asyncio.create_task(pg_store.claim_graph_intents(owner="a", lease_seconds=30))
        await asyncio.sleep(0.3)
        assert not worker_a.done(), "A must wait for B's claim to commit"
        await tx.commit()
    claimed = await asyncio.wait_for(worker_a, timeout=10)
    assert [r.effect_key for r in claimed] == []
    older = await pg_store.get_graph_intent(TENANT, "v1")
    assert older is not None and older.state == "superseded"


async def test_a_candidate_another_worker_holds_does_not_end_the_scan(db: None) -> None:
    """Two due subjects, limit 1: the first is locked elsewhere, the second is claimed."""
    await _save(_spec("held", entity_id="d-1"), entity_id="d-1")
    await _save(_spec("free", entity_id="d-2"), entity_id="d-2")
    engine = pg_store._engine
    assert engine is not None
    async with engine.connect() as other:
        tx = await other.begin()
        await other.execute(
            text("SELECT id FROM graph_sync_intents WHERE effect_key = 'held' FOR UPDATE")
        )
        claimed = await pg_store.claim_graph_intents(owner="a", lease_seconds=30, limit=1)
        await tx.rollback()
    assert [r.effect_key for r in claimed] == ["free"]


async def test_versions_are_ordered_by_resolved_endpoint_not_node_label(db: None) -> None:
    """`node_label=None` and `Depot` that resolve to one endpoint are one subject."""
    await _save(_spec("v1", node_label=None))
    await _save(_spec("v2", node_label="Depot"))
    claimed = await pg_store.claim_graph_intents(owner="w1", lease_seconds=30)
    assert [r.effect_key for r in claimed] == ["v1"]


async def test_held_origins_are_neither_claimed_nor_resolved(db: None) -> None:
    await _save(_spec("c1", origin="converge"))
    await _save(_spec("e1", entity_id="d-2", origin="enrich"), entity_id="d-2")
    claimed = await pg_store.claim_graph_intents(
        owner="w1", lease_seconds=30, held_origins=frozenset({"converge"})
    )
    assert [r.effect_key for r in claimed] == ["e1"]
    held = await pg_store.get_graph_intent(TENANT, "c1")
    assert held is not None and (held.state, held.attempts) == ("pending", 0)


async def test_a_mismatched_replay_attaches_no_intent(db: None) -> None:
    await _save(None, idempotency_key="caller-1")
    with (
        pg_store.graph_intent_scope(_spec(fields={"capacity": 9.9})),
        pytest.raises(pg_store.GraphIntentReplayMismatchError),
    ):
        await pg_store.save_enrichment_result(
            tenant_id=TENANT,
            entity_id="d-1",
            object_type="Depot",
            fields={"capacity": 9.9},
            confidence=0.9,
            uncertainty_score=0.1,
            tokens_used=1,
            processing_time_ms=1,
            pass_count=1,
            idempotency_key="caller-1",
        )
    assert await _count("graph_sync_intents") == 0


async def test_other_subjects_are_not_held_back(db: None) -> None:
    await _save(_spec("a1", entity_id="d-1"), entity_id="d-1")
    await _save(_spec("b1", entity_id="d-2"), entity_id="d-2")
    claimed = await pg_store.claim_graph_intents(owner="w1", lease_seconds=30)
    assert {r.effect_key for r in claimed} == {"a1", "b1"}


# ── Through the coordinator: restart and cross-process duplicates ───────────

ACK = {"status": "success", "entity_type": "depots", "synced_count": 1}
SETTINGS = SimpleNamespace(gate_url="https://gate.example")


def _router(*acks: Any) -> MagicMock:
    queue = list(acks)

    async def notify(**kwargs: Any) -> Any:
        ack = queue.pop(0) if queue else None
        kind = GraphSyncOutcomeKind.ACKNOWLEDGED if ack else GraphSyncOutcomeKind.RETRYABLE
        kwargs["on_outcome"](GraphSyncOutcome(kind, "test", ack))
        return ack

    router = MagicMock()
    router.notify_graph_sync = AsyncMock(side_effect=notify)
    router.graph_sync_pin = MagicMock(
        return_value=GraphSyncPin(entity_type="depots", contract_digest="c" * 64)
    )
    return router


async def _commit(coord: SideEffectCoordinator, router: MagicMock, emitter: MagicMock) -> Any:
    with (
        patch("app.engines.packet_router.get_router", return_value=router),
        patch("app.services.event_emitter.get_emitter", return_value=emitter),
    ):
        return await coord.commit_after_enrich(
            tenant=TENANT,
            entity_id="d-1",
            object_type="Depot",
            domain=TENANT,
            response_dict={"state": "completed", "fields": {"capacity": 2.5}, "confidence": 0.9},
            settings=SETTINGS,
            idempotency_key="op-1",
            node_label="Depot",
            graph_sync=False,
            defer_graph_sync=True,
            require_persistence=True,
        )


def _emitter() -> MagicMock:
    emitter = MagicMock()
    emitter.emit_enrichment_completed = AsyncMock()
    return emitter


async def test_restart_before_dispatch_recovers_from_postgres(db: None) -> None:
    """P15: the obligation outlives the process that answered `completed`."""
    emitter = _emitter()
    report = await _commit(SideEffectCoordinator(), _router(), emitter)
    assert report.persisted and report.graph_durable and report.graph_deferred
    restarted = SideEffectCoordinator()
    router = _router(ACK)
    with patch("app.engines.packet_router.get_router", return_value=router):
        assert await restarted.recover_graph_intents(SETTINGS) == 1
        assert await restarted.recover_graph_intents(SETTINGS) == 0
    assert router.notify_graph_sync.await_args.kwargs["fields"] == {"capacity": 2.5}
    intent = await pg_store.get_graph_intent(TENANT, report.key)
    assert intent is not None and intent.state == "acknowledged"
    assert await _count("enrichment_results") == 1
    assert emitter.emit_enrichment_completed.await_count == 1


async def test_ack_lost_then_restart_replays_once(db: None) -> None:
    """P16: one graph state, no duplicate persistence or completion event."""
    emitter = _emitter()
    first = SideEffectCoordinator()
    report = await _commit(first, _router(), emitter)
    with patch("app.engines.packet_router.get_router", return_value=_router(None)):
        await first.recover_graph_intents(SETTINGS)  # CEG wrote; the ack was lost
    intent = await pg_store.get_graph_intent(TENANT, report.key)
    assert intent is not None and intent.state == "pending" and intent.attempts == 1
    async with pg_store.get_session() as session:  # the backoff has elapsed
        await session.execute(text("UPDATE graph_sync_intents SET next_attempt_at = now()"))
    restarted = SideEffectCoordinator()
    router = _router(ACK)
    with patch("app.engines.packet_router.get_router", return_value=router):
        await restarted.recover_graph_intents(SETTINGS)
    intent = await pg_store.get_graph_intent(TENANT, report.key)
    assert intent is not None and intent.state == "acknowledged" and intent.attempts == 2
    again = await _commit(SideEffectCoordinator(), _router(ACK), emitter)
    assert again.skipped is True
    assert await _count("enrichment_results") == 1
    assert emitter.emit_enrichment_completed.await_count == 1


async def test_two_processes_completing_one_operation_commit_once(db: None) -> None:
    """P20/P25 through the coordinator, on the real store."""
    emitter = _emitter()
    a, b = await asyncio.gather(
        _commit(SideEffectCoordinator(), _router(), emitter),
        _commit(SideEffectCoordinator(), _router(), emitter),
    )
    assert sorted([a.skipped, b.skipped]) == [False, True]
    assert await _count("enrichment_results") == 1
    assert await _count("graph_sync_intents") == 1
    assert emitter.emit_enrichment_completed.await_count == 1


# ── EIE-D-004 on PostgreSQL: a configured store with no intent sends nothing ─


async def test_intent_deleted_before_the_deferred_leg_is_not_sent_unclaimed(db: None) -> None:
    """Rule 8 on the real store: the row is gone, so the leg has no owner and sends nothing."""
    coord = SideEffectCoordinator()
    report = await _commit(coord, _router(), _emitter())
    assert report.persisted and report.graph_durable and report.graph_deferred
    async with pg_store.get_session() as session:
        await session.execute(text("DELETE FROM graph_sync_intents"))
    assert await pg_store.get_graph_intent(TENANT, report.key) is None

    router = _router(ACK)
    with patch("app.engines.packet_router.get_router", return_value=router):
        assert coord.schedule_graph_leg(report.key, settings=SETTINGS)
        await coord.drain_graph_legs()

    router.notify_graph_sync.assert_not_awaited()


async def test_a_leg_for_a_key_the_store_never_recorded_sends_nothing(db: None) -> None:
    """No row was ever written for the key: nothing is sent and the leg ends terminal."""
    from app.services.side_effect_coordinator import SideEffectReport

    coord = SideEffectCoordinator()
    report = SideEffectReport(key="never-recorded")
    router = _router(ACK)
    with patch("app.engines.packet_router.get_router", return_value=router):
        await coord._graph_leg(
            key="never-recorded",
            tenant=TENANT,
            entity_id="d-1",
            fields={"capacity": 2.5},
            domain=TENANT,
            node_label="Depot",
            contract_digest="c" * 64,
            settings=SETTINGS,
            report=report,
        )

    router.notify_graph_sync.assert_not_awaited()
    assert report.graph_terminal and not report.graph_synced and not report.graph_deferred
    assert "graph_sync:no_durable_intent" in report.errors
    assert await _count("graph_sync_intents") == 0
