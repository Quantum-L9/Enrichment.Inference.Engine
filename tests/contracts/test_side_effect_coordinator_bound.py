"""Contract-bound surface for TASK-021 side-effect coordinator.

RV-DOMAIN-RD-07 binds the graph leg to CEG's acknowledgment: `graph_synced` is
set only when Cognitive.Engine.Graphs confirms the intended `sync` operation,
and an unacknowledged graph leg leaves the key retryable for that leg alone.
The contract these tests pin is documented in
docs/contracts/dependencies/constellation-gate.yaml (invariants); the retry
behaviour itself is exercised in tests/unit/test_side_effect_coordinator.py.
"""

from __future__ import annotations

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
    assert graph_sync_acknowledged(CEG_SYNC_OK, expected_count=1)
    assert not graph_sync_acknowledged(
        {"status": "success", "entity_type": "facilities", "synced_count": 2}, expected_count=1
    ), "an acknowledgment for a different batch size is not this request's"
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
    """A fresh report is neither synced nor a graph-only retry; the pending map is empty."""
    report = SideEffectReport(key="k")
    assert report.graph_retry is False
    assert report.graph_synced is False
    assert SideEffectCoordinator()._graph_pending == {}
