"""EIE-D-004: with a durable store configured, no claimed intent means no send.

L9_META:
  tier: 2
  domain: graph-sync
  authority: docs/contracts/data/graph_sync_intents.md rule 8 (ownership before sending)

Rule 8: "A store that cannot confirm a claim sends nothing for the key; recovery
sends it. Only a process with no durable store configured sends unclaimed."
`SideEffectCoordinator._lease_intent` instead answered "may send, unclaimed"
when the store was configured, the claim came back empty and no row existed --
the same answer it gives a process with no store at all. That send carries no
pinned contract digest, no `seq` (`sync_version`) and is settled on nothing.

The store double is the durability suite's `FakeIntents`, which has
`pg_store`'s claim/get/settle semantics in memory. Real PostgreSQL claims are
covered by tests/integration/test_graph_intent_store_pg.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from app.services.side_effect_coordinator import SideEffectCoordinator
from tests.unit.test_graph_intent_durability import (
    ACK,
    SETTINGS,
    TENANT,
    _commit,
    _router,
    _world,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def _inline_leg(coord: SideEffectCoordinator, router: Any, key: str = "k-1") -> Any:
    """One graph leg for `key`, as the inline commit path runs it."""
    from app.services.side_effect_coordinator import SideEffectReport

    report = SideEffectReport(key=key)
    with patch("app.engines.packet_router.get_router", return_value=router):
        await coord._graph_leg(
            key=key,
            tenant=TENANT,
            entity_id="d-1",
            fields={"capacity": 2.5},
            domain=TENANT,
            node_label="Depot",
            contract_digest="digest-1",
            settings=SETTINGS,
            report=report,
        )
    return report


async def test_configured_store_with_no_intent_row_sends_nothing() -> None:
    world = _world()
    assert world.intents.configured() and not world.intents.rows
    coord = SideEffectCoordinator(intents=world.intents)
    router = _router(ACK)

    report = await _inline_leg(coord, router)

    router.notify_graph_sync.assert_not_awaited()
    assert not report.graph_synced
    assert any("no_durable_intent" in e for e in report.errors), report.errors


async def test_missing_intent_is_reported_pending_not_synced_or_terminal() -> None:
    """Nothing was sent and CEG refused nothing: the key stays open for a later call."""
    world = _world()
    coord = SideEffectCoordinator(intents=world.intents)

    report = await _inline_leg(coord, _router(ACK))

    assert report.graph_deferred
    assert not report.graph_terminal


async def test_an_intent_deleted_after_a_deferred_commit_is_not_sent_unclaimed() -> None:
    """The deferred leg runs later; if its row is gone by then, nothing is sent."""
    world = _world()
    coord = SideEffectCoordinator(intents=world.intents)
    await _commit(
        coord, world, _router(), idempotency_key="op-1", graph_sync=False, defer_graph_sync=True
    )
    key = world.intents.only().effect_key
    world.intents.rows.clear()  # the durable obligation is gone (deleted, purged)

    router = _router(ACK)
    with patch("app.engines.packet_router.get_router", return_value=router):
        assert coord.schedule_graph_leg(key, settings=SETTINGS)
        await coord.drain_graph_legs()

    router.notify_graph_sync.assert_not_awaited()
    assert key not in coord._completed


async def test_a_failed_optional_persist_does_not_send_the_graph_leg_unclaimed() -> None:
    """Persistence failed (not required): no intent row was written, so no send."""
    world = _world()
    world.persist.side_effect = RuntimeError("db write failed")
    coord = SideEffectCoordinator(intents=world.intents)
    router = _router(ACK)

    report = await _commit(coord, world, router, idempotency_key="op-1")

    assert not report.persisted
    router.notify_graph_sync.assert_not_awaited()
    assert not report.graph_synced


# ── Controls: what must keep working ───────────────────────────────────────


async def test_control_no_durable_store_still_sends_unclaimed() -> None:
    world = _world()
    world.intents.configured_store = False
    coord = SideEffectCoordinator(intents=world.intents)
    router = _router(ACK)

    report = await _inline_leg(coord, router)

    assert router.notify_graph_sync.await_count == 1
    assert report.graph_synced


async def test_control_a_leased_intent_sends_its_pinned_fields_and_seq() -> None:
    world = _world()
    coord = SideEffectCoordinator(intents=world.intents)
    router = _router(ACK)

    report = await _commit(coord, world, router, idempotency_key="op-1", fields={"capacity": 7.0})

    assert report.graph_synced and report.graph_durable
    (call,) = router.notify_graph_sync.await_args_list
    row = world.intents.only()
    assert call.kwargs["fields"] == row.fields == {"capacity": 7.0}
    assert call.kwargs["sync_version"] == row.seq
    assert row.state == "acknowledged"
