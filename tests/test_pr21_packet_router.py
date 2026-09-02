"""
tests/test_pr21_packet_router.py

Proves GAP-1 and GAP-6:
  - dispatch_to_score does NOT exist in packet_router (confirms broken ref is gone)
  - notify_graph_sync builds a valid TransportPacket and sends it through Gate
  - notify_score_invalidate fires a Gate-routed fire-and-forget packet with correct action
  - get_router() returns a singleton PacketRouter
  - orchestration_layer no longer imports the non-existent dispatch_to_score
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from constellation_node_sdk.transport import TransportPacket, create_transport_packet

from app.engines.packet_router import (
    NodeTarget,
    NodeUnreachableError,
    PacketRouter,
    _build_envelope,
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


def test_build_envelope_structure():
    """TransportPacket must contain canonical header, tenant, and security hashes."""
    packet = _build_envelope(
        action="graph-sync",
        tenant_id="tenant-x",
        payload={"entity_id": "ent-001", "fields": {"material_type": "HDPE"}},
    )
    assert isinstance(packet, TransportPacket)
    assert packet.header.action == "graph-sync"
    assert packet.tenant.org_id == "tenant-x"
    assert packet.header.packet_id
    assert packet.security.payload_hash
    assert packet.security.transport_hash


@pytest.mark.asyncio
async def test_notify_graph_sync_sends_to_gate():
    """GAP-6: notify_graph_sync must send a TransportPacket to Gate."""
    router = PacketRouter(gate_url="https://gate-node:8080")

    response_packet = create_transport_packet(
        action="graph-sync",
        payload={"status": "synced"},
        tenant="tenant-acme",
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )

    with patch.object(
        router._client, "send_to_gate", new_callable=AsyncMock, return_value=response_packet
    ) as mock_send:
        result = await router.notify_graph_sync(
            tenant_id="tenant-acme",
            entity_id="ent-001",
            fields={"material_type": "HDPE", "mfi_range": "2-4"},
            domain="plastics",
        )
        assert result["status"] == "synced"
        assert "packet_id" in result
        sent_packet = mock_send.await_args.args[0]
    assert isinstance(sent_packet, TransportPacket)
    assert sent_packet.header.action == "graph-sync"
    assert sent_packet.tenant.org_id == "tenant-acme"
    assert sent_packet.payload["entity_id"] == "ent-001"
    assert sent_packet.payload["domain"] == "plastics"


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


@pytest.mark.asyncio
async def test_route_builds_a_fresh_packet_per_attempt(monkeypatch):
    """Gate's replay guard rejects a reused packet_id, so a retry must not resend the same packet."""
    from app.engines import packet_router as pr_module

    router = PacketRouter(gate_url="https://gate-node:9000")
    seen: list[str] = []

    response_packet = create_transport_packet(
        action="graph-sync",
        payload={"status": "synced"},
        tenant="tenant-acme",
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )

    async def send(packet):
        seen.append(str(packet.header.packet_id))
        if len(seen) == 1:
            raise RuntimeError("transient")
        return response_packet

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(pr_module.asyncio, "sleep", no_sleep)
    with patch.object(router._client, "send_to_gate", side_effect=send):
        result = await router.route(
            NodeTarget.GRAPH,
            "graph-sync",
            "tenant-acme",
            {"entity_id": "ent-001"},
        )

    assert result["status"] == "synced"
    assert len(seen) == 2
    assert seen[0] != seen[1], "retry reused the packet_id Gate's replay guard already saw"


@pytest.mark.asyncio
async def test_route_does_not_retry_a_gate_4xx(monkeypatch):
    """A 404 (no owner for the action) is not transient; retrying it only burns Gate calls."""
    from constellation_node_sdk.gate import GateHTTPError

    from app.engines import packet_router as pr_module

    router = PacketRouter(gate_url="https://gate-node:9000")
    calls = {"n": 0}
    sleeps: list[float] = []

    async def send(packet):
        calls["n"] += 1
        raise GateHTTPError("no route", status_code=404, response_text="{}")

    async def record_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(pr_module.asyncio, "sleep", record_sleep)
    with patch.object(router._client, "send_to_gate", side_effect=send):
        with pytest.raises(NodeUnreachableError):
            await router.route(NodeTarget.SCORE, "score-invalidate", "tenant-x", {"entity_id": "e"})

    assert calls["n"] == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_route_still_retries_a_gate_5xx(monkeypatch):
    from constellation_node_sdk.gate import GateHTTPError

    from app.engines import packet_router as pr_module

    router = PacketRouter(gate_url="https://gate-node:9000")
    calls = {"n": 0}

    async def send(packet):
        calls["n"] += 1
        raise GateHTTPError("gateway timeout", status_code=504, response_text="{}")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(pr_module.asyncio, "sleep", no_sleep)
    with patch.object(router._client, "send_to_gate", side_effect=send):
        with pytest.raises(NodeUnreachableError):
            await router.route(NodeTarget.SCORE, "score-invalidate", "tenant-x", {"entity_id": "e"})

    assert calls["n"] == 3
