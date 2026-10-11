"""
tests/test_pr21_packet_router.py

Proves GAP-1 and GAP-6:
  - dispatch_to_score does NOT exist in packet_router (confirms broken ref is gone)
  - notify_graph_sync speaks CEG's `sync` contract and sends it through Gate
  - notify_score_invalidate fires a Gate-routed fire-and-forget packet with correct action
  - get_router() returns a singleton PacketRouter
  - orchestration_layer no longer imports the non-existent dispatch_to_score
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from constellation_node_sdk.transport import create_transport_packet

from app.engines.packet_router import (
    NodeTarget,
    NodeUnreachableError,
    PacketRouter,
    _graph_sync_row,
    _retry_allowed,
    get_router,
)


def test_dispatch_to_score_symbol_absent():
    """GAP-1: dispatch_to_score must not exist in packet_router."""
    import app.engines.packet_router as pr

    assert not hasattr(pr, "dispatch_to_score"), (
        "dispatch_to_score must be absent — orchestration_layer must use "
        "get_router().notify_score_invalidate() instead"
    )


def test_orchestration_layer_does_not_import_dispatch_to_score():
    """GAP-1: orchestration_layer source must not contain a dispatch_to_score import."""
    import ast
    import pathlib

    src_path = pathlib.Path("app/engines/orchestration_layer.py")
    if not src_path.exists():
        pytest.skip("orchestration_layer.py not found in cwd")
    tree = ast.parse(src_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.names:
                names = [alias.name for alias in node.names]
                assert "dispatch_to_score" not in names, (
                    f"Stale import 'dispatch_to_score' found at line {node.lineno}"
                )


def test_graph_sync_row_is_a_ceg_sync_row_of_neo4j_primitives():
    """Rows carry the CEG endpoint id property; nested values are JSON strings."""
    row = _graph_sync_row(
        entity_id="ent-001",
        fields={"material_type": "HDPE", "nested": {"a": 1}, "tags": ["x", "y"]},
        domain="plastics",
        id_property="facility_id",
    )
    assert row["facility_id"] == "ent-001"
    assert row["entity_id"] == "ent-001"
    assert row["domain"] == "plastics"
    assert row["material_type"] == "HDPE"
    assert row["tags"] == ["x", "y"]
    assert row["nested"] == '{"a": 1}'
    assert row["enriched_by"] == "enrichment-engine"


@pytest.mark.asyncio
async def test_notify_graph_sync_sends_ceg_sync_contract_to_gate():
    """GAP-6 successor: graph sync is CEG's `sync` action, sent through Gate."""
    router = PacketRouter(gate_url="https://gate-node:8080")

    response_packet = create_transport_packet(
        action="sync",
        payload={"status": "success", "entity_type": "facilities", "synced_count": 1},
        tenant="tenant-acme",
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )

    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=response_packet
    ) as mock_exec:
        result = await router.notify_graph_sync(
            tenant_id="tenant-acme",
            entity_id="ent-001",
            fields={"material_type": "HDPE", "mfi_range": "2-4"},
            domain="plastics",
        )
        assert result["status"] == "success"
        assert "packet_id" in result
        kwargs = mock_exec.await_args.kwargs
    assert kwargs["action"] == "sync"
    assert kwargs["tenant"] == "tenant-acme"
    assert kwargs["payload"]["entity_type"] == "facilities"
    assert kwargs["payload"]["batch"][0]["facility_id"] == "ent-001"
    assert kwargs["payload"]["batch"][0]["domain"] == "plastics"
    assert kwargs["idempotency_key"].startswith("eie:sync:tenant-acme:ent-001:")
    assert kwargs["timeout_ms"] == 10_000


def test_retry_discipline_never_multiplies_a_domain_effect():
    """One retry owner (EIE), and only where a retry cannot duplicate work."""
    from constellation_node_sdk.gate import (
        GateConnectionError,
        GateHTTPError,
        GatePolicyError,
        GateTimeoutError,
    )

    assert _retry_allowed(GateConnectionError("x"), has_idempotency_key=False)
    assert not _retry_allowed(GateTimeoutError("x"), has_idempotency_key=False)
    assert _retry_allowed(GateTimeoutError("x"), has_idempotency_key=True)
    assert not _retry_allowed(GateHTTPError("x", status_code=404), has_idempotency_key=True)
    assert not _retry_allowed(GateHTTPError("x", status_code=502), has_idempotency_key=False)
    assert _retry_allowed(GateHTTPError("x", status_code=502), has_idempotency_key=True)
    assert not _retry_allowed(GatePolicyError("x"), has_idempotency_key=True)


@pytest.mark.asyncio
async def test_unknown_route_fails_closed_without_direct_fallback():
    """A 404 from Gate (no owner for the action) is terminal: one attempt, no peer call."""
    from constellation_node_sdk.gate import GateHTTPError

    router = PacketRouter(gate_url="https://gate-node:8080")
    with patch.object(
        router._client,
        "execute",
        new_callable=AsyncMock,
        side_effect=GateHTTPError("no route", status_code=404),
    ) as mock_exec:
        with pytest.raises(NodeUnreachableError):
            await router.route(NodeTarget.SCORE, "score-invalidate", "t", {"entity_id": "e"})
        assert mock_exec.await_count == 1


def test_notify_score_invalidate_is_fire_and_forget():
    """GAP-1: notify_score_invalidate must call route_fire_and_forget (non-blocking)."""
    router = PacketRouter(gate_url="https://gate-node:8080")

    fired: list = []

    def capture_fff(target, action, tenant_id, payload, correlation_id=None):
        fired.append({"target": target, "action": action, "tenant_id": tenant_id})

    router.route_fire_and_forget = capture_fff

    import asyncio

    asyncio.run(
        router.notify_score_invalidate(tenant_id="tenant-x", entity_id="ent-007", domain="plastics")
    )

    assert len(fired) == 1
    assert fired[0]["action"] == "score-invalidate"
    assert fired[0]["tenant_id"] == "tenant-x"
    assert fired[0]["target"] == NodeTarget.SCORE


def test_get_router_returns_singleton():
    """get_router() must return the same PacketRouter instance per process."""

    class SettingsStub:
        gate_url = "https://gate:8080"

    settings = SettingsStub()

    r1 = get_router(settings)
    r2 = get_router(settings)
    assert r1 is r2, "get_router must be a singleton (lru_cache)"


# ── RV-DOMAIN-RD-07: a worker failure is not a routed success ──


def _ceg_response(payload: dict) -> object:
    return create_transport_packet(
        action="sync",
        payload=payload,
        tenant="tenant-acme",
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )


def _ceg_failure_packet(*, with_hop: bool = False) -> object:
    """What Gate relays (HTTP 200) when CEG's handler raised.

    Gate_SDK ``create_error_transport_packet``: ``packet_type="failure"``, payload
    ``{"status": "failed", "error": ..., "message": ..., "packet_id": ...}`` and a
    response hop with ``status="failed"``.
    """
    from constellation_node_sdk.transport.hop_trace import make_response_hop

    request = _ceg_response({"entity_type": "facilities", "batch": [{"facility_id": "ent-001"}]})
    failure = request.derive(
        packet_type="failure",
        source_node="graph-engine",
        destination_node="enrichment-engine",
        reply_to="graph-engine",
        payload={
            "status": "failed",
            "error": "ExecutionError",
            "message": "Sync query execution failed",
            "packet_id": str(request.header.packet_id),
        },
    )
    if with_hop:
        failure = failure.with_hop(
            make_response_hop(
                packet=failure,
                node="graph-engine",
                action="sync",
                status="failed",
                error_code="ExecutionError",
            )
        )
    return failure


@pytest.mark.asyncio
async def test_route_raises_on_worker_failure_packet_without_retrying():
    """A `failure` packet is the worker saying no; `route` must not return it as data."""
    from app.engines.packet_router import NodeOperationFailedError

    router = PacketRouter(gate_url="https://gate-node:8080")
    with patch.object(
        router._client,
        "execute",
        new_callable=AsyncMock,
        return_value=_ceg_failure_packet(with_hop=True),
    ) as mock_exec:
        with pytest.raises(NodeOperationFailedError) as excinfo:
            await router.route(
                NodeTarget.GRAPH,
                "sync",
                "tenant-acme",
                {"entity_type": "facilities", "batch": [{"facility_id": "ent-001"}]},
                idempotency_key="eie:sync:tenant-acme:ent-001:abc",
            )
        assert mock_exec.await_count == 1, "a worker failure is not a transport retry case"
    assert isinstance(excinfo.value, NodeUnreachableError), "existing handlers keep catching it"
    assert "ExecutionError" in str(excinfo.value)


@pytest.mark.asyncio
async def test_response_hop_failed_status_is_a_failure_even_with_response_packet_type():
    """Gate's second terminal-failure signal: a response hop recorded as `failed`."""
    from constellation_node_sdk.transport.hop_trace import make_response_hop

    from app.engines.packet_router import NodeOperationFailedError

    request = _ceg_response({"entity_type": "facilities", "batch": [{"facility_id": "ent-001"}]})
    packet = request.derive(
        packet_type="response",
        source_node="graph-engine",
        destination_node="enrichment-engine",
        reply_to="graph-engine",
        payload={"status": "success", "entity_type": "facilities", "synced_count": 1},
    )
    packet = packet.with_hop(
        make_response_hop(
            packet=packet,
            node="graph-engine",
            action="sync",
            status="failed",
            error_code="ExecutionError",
        )
    )
    router = PacketRouter(gate_url="https://gate-node:8080")
    with (
        patch.object(router._client, "execute", new_callable=AsyncMock, return_value=packet),
        pytest.raises(NodeOperationFailedError),
    ):
        await router.route(NodeTarget.GRAPH, "sync", "tenant-acme", {"batch": []})


@pytest.mark.asyncio
async def test_notify_graph_sync_returns_none_on_worker_failure_packet():
    """Reproduction of RV-DOMAIN-RD-07 at the router seam.

    Before the repair the failure payload ``{"status": "failed", ...}`` came back
    as a truthy dict and the coordinator recorded the row as synced.
    """
    router = PacketRouter(gate_url="https://gate-node:8080")
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_ceg_failure_packet()
    ) as mock_exec:
        result = await router.notify_graph_sync(
            tenant_id="tenant-acme", entity_id="ent-001", fields={"x": 1}, domain="plastics"
        )
        assert mock_exec.await_count == 1
        kwargs = mock_exec.await_args.kwargs
    assert result is None
    # The request that went out is still CEG's `sync` contract through Gate.
    assert kwargs["action"] == "sync"
    assert kwargs["payload"]["batch"][0]["facility_id"] == "ent-001"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "status": "all_rejected",
            "entity_type": "facilities",
            "synced_count": 0,
            "rejected_count": 1,
        },
        {"status": "success", "entity_type": "facilities", "synced_count": 0},
        {"status": "success", "entity_type": "outcome", "synced_count": 1},
        {"status": "success", "entity_type": "facilities"},
        {"status": "failed", "code": "execution_failed"},
        {},
    ],
    ids=["all-rejected", "zero-synced", "other-endpoint", "no-count", "failed-status", "empty"],
)
async def test_notify_graph_sync_returns_none_when_ceg_does_not_confirm_the_operation(payload):
    router = PacketRouter(gate_url="https://gate-node:8080")
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_ceg_response(payload)
    ):
        result = await router.notify_graph_sync(
            tenant_id="tenant-acme", entity_id="ent-001", fields={}, domain="plastics"
        )
    assert result is None


@pytest.mark.asyncio
async def test_notify_graph_sync_returns_the_confirmed_acknowledgment():
    router = PacketRouter(gate_url="https://gate-node:8080")
    ok = {"status": "success", "entity_type": "facilities", "synced_count": 1}
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_ceg_response(ok)
    ):
        result = await router.notify_graph_sync(
            tenant_id="tenant-acme", entity_id="ent-001", fields={}, domain="plastics"
        )
    assert result is not None
    assert result["status"] == "success"
    assert result["synced_count"] == 1
    assert "packet_id" in result


def test_graph_sync_acknowledged_is_ceg_handle_sync_contract():
    """The predicate accepts exactly CEG's success shape, for the intended endpoint."""
    from app.engines.packet_router import graph_sync_acknowledged as ok

    assert ok({"status": "success", "entity_type": "facilities", "synced_count": 1})
    assert ok(
        {"status": "success", "entity_type": "facilities", "synced_count": 3},
        entity_type="facilities",
    )
    assert ok({"status": "success", "synced_count": 1, "rejected_count": 2, "packet_id": "p"})
    assert not ok(
        {"status": "success", "entity_type": "outcome", "synced_count": 1}, entity_type="facilities"
    )
    assert not ok({"status": "all_rejected", "entity_type": "facilities", "synced_count": 0})
    assert not ok({"status": "failed", "code": "execution_failed"})
    assert not ok({"status": "success", "entity_type": "facilities", "synced_count": 0})
    assert not ok({"status": "success", "entity_type": "facilities", "synced_count": True})
    assert not ok({"status": "success", "entity_type": "facilities", "synced_count": "1"})
    assert not ok({"status": "success", "entity_type": "facilities"})
    assert not ok({"status": "ok"})
    assert not ok(None)
    assert not ok({})
    assert not ok("success")
    assert not ok(["success"])
