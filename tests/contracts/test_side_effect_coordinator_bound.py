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


# ── WU-01 / D-7: operation identity comes from the transport header ───────


def _sdk_packet(payload: dict, packet_id: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        tenant=SimpleNamespace(org_id="acme"),
        payload=payload,
        header=SimpleNamespace(packet_id=packet_id),
    )


async def test_enrich_keys_side_effects_on_the_header_packet_id_not_the_payload() -> None:
    """D-7: the SDK three-argument call hands the handler the immutable header;
    a payload `packet_id` cannot name (or spoof) the operation."""
    from unittest.mock import AsyncMock, patch

    from constellation_node_sdk.runtime.execution import _invoke_handler

    from app.engines import handlers
    from app.models.schemas import EnrichResponse

    payload = {
        "entity_id": "res.partner:1",
        "entity": {"id": "res.partner:1", "name": "Acme"},
        "object_type": "Account",
        "objective": "x",
        "packet_id": "spoofed-by-the-caller",
    }
    commit = AsyncMock()
    with (
        patch.object(
            handlers,
            "enrich_entity",
            AsyncMock(return_value=EnrichResponse(state="completed", fields={"a": 1})),
        ),
        patch(
            "app.services.side_effect_coordinator.SideEffectCoordinator.commit_after_enrich",
            commit,
        ),
    ):
        await _invoke_handler(handlers.handle_enrich, _sdk_packet(payload, "hdr-123"))

    assert commit.await_args.kwargs["packet_id"] == "hdr-123"


async def test_direct_in_process_enrich_ignores_a_payload_packet_id() -> None:
    from unittest.mock import AsyncMock, patch

    from app.engines import handlers
    from app.models.schemas import EnrichResponse

    commit = AsyncMock()
    with (
        patch.object(
            handlers,
            "enrich_entity",
            AsyncMock(return_value=EnrichResponse(state="completed", fields={"a": 1})),
        ),
        patch(
            "app.services.side_effect_coordinator.SideEffectCoordinator.commit_after_enrich",
            commit,
        ),
    ):
        await handlers.handle_enrich(
            "acme",
            {
                "entity_id": "res.partner:1",
                "entity": {"id": "res.partner:1"},
                "object_type": "Account",
                "objective": "x",
                "packet_id": "spoofed-by-the-caller",
            },
        )

    assert commit.await_args.kwargs["packet_id"] is None


# ── WU-01 / F-037 F-048: the Odoo compatibility path ──────────────────────


def test_compat_runs_with_different_correlation_ids_key_differently() -> None:
    from app.services.odoo_gate_converge import _idempotency_key

    a = _idempotency_key({"entity_id": "res.partner:7", "odoo": {"correlation_id": "run:1"}})
    b = _idempotency_key({"entity_id": "res.partner:7", "odoo": {"correlation_id": "run:2"}})
    assert a != b


def test_compat_run_without_correlation_id_does_not_collapse_onto_the_entity() -> None:
    from app.services.odoo_gate_converge import _idempotency_key

    assert _idempotency_key({"entity_id": "res.partner:7", "odoo": {}}) is None
    assert _idempotency_key({"entity_id": "res.partner:7", "odoo": {"correlation_id": 7}}) is None


async def test_compat_persist_failure_is_a_failed_converge() -> None:
    """F-048: same launch invariant as canonical — no durable result, no 'completed'."""
    from unittest.mock import AsyncMock, MagicMock, patch

    import pytest

    from app.engines import handlers
    from app.models.schemas import EnrichResponse
    from app.services.odoo_gate_converge import parse_odoo_converge_request
    from app.services.side_effect_coordinator import PersistenceRequiredError

    # The compat dialect, as tests/unit/test_odoo_gate_converge.py::_odoo_payload.
    payload = {
        "entity_id": "res.partner:55",
        "domain": "plasticos",
        "entity_snapshot": {"name": "Acme Recycling", "city": "Charlotte"},
        "odoo": {
            "model": "plasticos.enrichment.run",
            "record_id": 7,
            "correlation_id": "plasticos.enrichment.run:7",
        },
    }
    parsed = parse_odoo_converge_request(payload)

    persist = AsyncMock(side_effect=PersistenceRequiredError("store down"))
    with (
        patch.object(
            handlers,
            "run_convergence_loop",
            AsyncMock(
                return_value=EnrichResponse(state="completed", fields={"website": "https://a.x"})
            ),
        ),
        patch.object(handlers, "_persist_and_sync", persist),
        patch.object(handlers, "get_settings", return_value=MagicMock()),
        pytest.raises(RuntimeError, match="not durable"),
    ):
        await handlers._run_odoo_compat_converge("plasticos", payload, parsed, "run-1")

    assert persist.await_args.kwargs["require_persistence"] is True
