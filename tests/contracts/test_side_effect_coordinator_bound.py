"""Contract-bound surface for TASK-021 side-effect coordinator.

RV-DOMAIN-RD-07 binds the graph leg to CEG's acknowledgment: `graph_synced` is
set only when Cognitive.Engine.Graphs confirms the intended `sync` operation,
and an unacknowledged graph leg leaves the key retryable for that leg alone.
The contract these tests pin is documented in
docs/contracts/dependencies/constellation-gate.yaml (invariants).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.engines.packet_router import (
    NodeOperationFailedError,
    NodeUnreachableError,
    graph_sync_acknowledged,
)
from app.services.side_effect_coordinator import (
    SideEffectCoordinator,
    SideEffectReport,
    get_side_effect_coordinator,
    semantic_side_effect_key,
)

# CEG `handle_sync` success acknowledgment (Cognitive.Engine.Graphs engine/handlers.py).
CEG_SYNC_OK = {"status": "success", "entity_type": "facilities", "synced_count": 1}


def test_coordinator_singleton_stable() -> None:
    a = get_side_effect_coordinator()
    b = get_side_effect_coordinator()
    assert a is b
    assert isinstance(a, SideEffectCoordinator)


def test_semantic_key_contract() -> None:
    key = semantic_side_effect_key(tenant="t", entity_id="e", packet_id="abc")
    assert key.startswith("pkt:abc")


# ── RV-DOMAIN-RD-07: acknowledgment contract ───────────────────


def test_graph_sync_acknowledgment_is_ceg_handle_sync_success_shape() -> None:
    """Exactly CEG's success acknowledgment confirms a sync; nothing weaker does."""
    assert graph_sync_acknowledged(CEG_SYNC_OK)
    assert graph_sync_acknowledged(CEG_SYNC_OK, entity_type="facilities")
    assert not graph_sync_acknowledged(CEG_SYNC_OK, entity_type="outcome")
    assert not graph_sync_acknowledged(None)
    assert not graph_sync_acknowledged({"status": "failed", "code": "execution_failed"})
    assert not graph_sync_acknowledged(
        {"status": "all_rejected", "entity_type": "facilities", "synced_count": 0}
    )
    assert not graph_sync_acknowledged({"status": "success", "entity_type": "facilities"})
    assert not graph_sync_acknowledged({"status": "ok"})


def test_worker_failure_is_a_node_unreachable_subclass() -> None:
    """Existing `except NodeUnreachableError` handlers keep catching a worker failure."""
    assert issubclass(NodeOperationFailedError, NodeUnreachableError)


def test_report_carries_graph_retry_marker() -> None:
    report = SideEffectReport(key="k")
    assert report.graph_retry is False
    assert report.graph_synced is False


def _collaborators(graph_ack):
    router = MagicMock()
    router.notify_graph_sync = AsyncMock(return_value=graph_ack)
    router.notify_score_invalidate = AsyncMock()
    emitter = MagicMock()
    emitter.emit_enrichment_completed = AsyncMock()
    return router, emitter


async def _commit(coord, router, emitter, persist, **kwargs):
    with (
        patch("app.models.schemas.EnrichResponse.model_validate", return_value=MagicMock()),
        patch("app.services.result_store.ResultStore") as result_store_cls,
        patch("app.engines.packet_router.get_router", return_value=router),
        patch("app.services.event_emitter.get_emitter", return_value=emitter),
    ):
        result_store_cls.return_value.persist_enrich_response = persist
        return await coord.commit_after_enrich(
            tenant="t1",
            entity_id="e1",
            object_type="Contact",
            domain="plasticos",
            response_dict={"state": "completed", "fields": {"x": 1}, "confidence": 0.9},
            settings=SimpleNamespace(gate_url="https://gate.example"),
            **kwargs,
        )


@pytest.mark.asyncio
async def test_unacknowledged_graph_leg_is_retryable_without_duplicating_effects() -> None:
    """Contract: an unsynced key is not completed; its retry runs the graph leg only."""
    coord = SideEffectCoordinator()
    router, emitter = _collaborators(graph_ack=None)
    persist = AsyncMock()

    first = await _commit(coord, router, emitter, persist, idempotency_key="rd07")
    assert first.graph_synced is False
    assert first.key not in coord._completed

    router.notify_graph_sync = AsyncMock(return_value=dict(CEG_SYNC_OK))
    second = await _commit(coord, router, emitter, persist, idempotency_key="rd07")
    assert second.skipped is False
    assert second.graph_retry is True
    assert second.graph_synced is True
    assert second.key in coord._completed

    assert persist.await_count == 1
    assert router.notify_score_invalidate.await_count == 1
    assert emitter.emit_enrichment_completed.await_count == 1


@pytest.mark.asyncio
async def test_graph_sync_false_boundary_is_preserved() -> None:
    """The canonical converge exclusion skips the hop and completes the key (EIE-004)."""
    coord = SideEffectCoordinator()
    router, emitter = _collaborators(graph_ack=None)

    report = await _commit(
        coord, router, emitter, AsyncMock(), idempotency_key="b", graph_sync=False
    )
    assert report.graph_skipped is True
    assert report.graph_synced is False
    assert router.notify_graph_sync.await_count == 0
    assert report.score_invalidated is True
    assert report.event_emitted is True
    assert report.key in coord._completed
