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

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from constellation_node_sdk.transport import create_transport_packet

import app.engines.packet_router as packet_router_module
from app.engines.domain_yaml_reader import DomainYamlReader
from app.engines.packet_router import (
    GRAPH_SYNC_SOURCE_DOMAIN_SPEC,
    GRAPH_SYNC_SOURCE_SETTINGS_FALLBACK,
    NodeTarget,
    NodeUnreachableError,
    PacketRouter,
    _graph_sync_row,
    _retry_allowed,
    get_router,
    resolve_graph_sync_contract,
)

# One tenant, one directory, one spec (identity contract 2026-10-22). The sync
# section is CEG's owner-native SyncEndpointSpec shape: path / targetnode /
# idproperty, with entity_type being the path's last segment.
_PLASTICS_SPEC = {
    "name": "plastics",
    "version": "0.3.0",
    "ontology": {"nodes": [{"label": "Facility", "properties": [{"name": "id"}]}]},
    "sync": {
        "endpoints": [
            {
                "path": "/v1/sync/facilities",
                "targetnode": "Facility",
                "idproperty": "facility_id",
                "batchstrategy": "UNWINDMERGE",
            },
            {
                "path": "/v1/sync/polymerfamilies",
                "targetnode": "PolymerFamily",
                "idproperty": "code",
                "batchstrategy": "UNWINDMERGE",
            },
        ]
    },
}


def _domains_dir(tmp_path: Path, specs: dict[str, dict]) -> Path:
    root = tmp_path / "domains"
    for domain, spec in specs.items():
        (root / domain).mkdir(parents=True)
        (root / domain / "spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    return root


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
async def test_notify_graph_sync_sends_ceg_sync_contract_to_gate(tmp_path):
    """GAP-6 successor: graph sync is CEG's `sync` action, sent through Gate.

    GRAPH-IDENT-02: the endpoint (`facilities`) and its id property
    (`facility_id`) come from the `plastics` domain's own spec, not a constant.
    """
    reader = DomainYamlReader(_domains_dir(tmp_path, {"plastics": _PLASTICS_SPEC}))
    router = PacketRouter(gate_url="https://gate-node:8080", domain_reader=reader)

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


# ── GRAPH-IDENT-02: per-domain sync contract from the domain spec ───────────


def test_sync_contract_is_read_from_the_domain_spec_by_ontology_label():
    """Two endpoints, one of which targets the domain's own ontology node: that one."""
    contract = resolve_graph_sync_contract(_PLASTICS_SPEC)
    assert contract is not None
    assert (contract.entity_type, contract.id_property) == ("facilities", "facility_id")
    assert contract.source == GRAPH_SYNC_SOURCE_DOMAIN_SPEC


def test_sync_contract_honours_an_explicit_node_label():
    contract = resolve_graph_sync_contract(_PLASTICS_SPEC, node_label="PolymerFamily")
    assert contract is not None
    assert (contract.entity_type, contract.id_property) == ("polymerfamilies", "code")
    assert resolve_graph_sync_contract(_PLASTICS_SPEC, node_label="Nope") is None


def test_sync_contract_single_endpoint_needs_no_label():
    spec = {"sync": {"endpoints": [{"path": "/v1/sync/metals/", "idproperty": "metal_id"}]}}
    contract = resolve_graph_sync_contract(spec)
    assert contract is not None
    assert (contract.entity_type, contract.id_property) == ("metals", "metal_id")


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {"sync": {}},
        {"sync": {"endpoints": []}},
        {"sync": {"endpoints": [{"path": "/v1/sync/facilities"}]}},  # no id property
        {  # two endpoints, neither selectable by label
            "sync": {
                "endpoints": [
                    {"path": "/v1/sync/a", "targetnode": "A", "idproperty": "a_id"},
                    {"path": "/v1/sync/b", "targetnode": "B", "idproperty": "b_id"},
                ]
            }
        },
    ],
)
def test_sync_contract_never_guesses(spec):
    """No section, no id property, or an ambiguous pair resolves to None, not a default."""
    assert resolve_graph_sync_contract(spec) is None


@pytest.mark.asyncio
async def test_graph_sync_fails_closed_for_a_domain_without_a_contract(tmp_path):
    """No spec endpoint and no fallback: nothing is sent, the skip is reported."""
    reader = DomainYamlReader(_domains_dir(tmp_path, {"plastics": _PLASTICS_SPEC}))
    router = PacketRouter(gate_url="https://gate-node:8080", domain_reader=reader)

    with patch.object(router._client, "execute", new_callable=AsyncMock) as mock_exec:
        result = await router.notify_graph_sync(
            tenant_id="tenant-acme",
            entity_id="ent-001",
            fields={"material_type": "HDPE"},
            domain="metals",  # no such spec
        )
    assert result is None
    mock_exec.assert_not_awaited()
    assert router.graph_sync_contract("metals") is None


@pytest.mark.asyncio
async def test_graph_sync_fallback_is_labeled_and_only_used_without_a_spec_contract(tmp_path):
    """Settings fallback stands in for a spec that declares none, and says so."""
    reader = DomainYamlReader(_domains_dir(tmp_path, {"plastics": _PLASTICS_SPEC}))
    router = PacketRouter(
        gate_url="https://gate-node:8080",
        domain_reader=reader,
        fallback_entity_type="facilities",
        fallback_id_property="facility_id",
    )

    fallback = router.graph_sync_contract("metals")
    assert fallback is not None
    assert fallback.source == GRAPH_SYNC_SOURCE_SETTINGS_FALLBACK
    assert (fallback.entity_type, fallback.id_property) == ("facilities", "facility_id")

    from_spec = router.graph_sync_contract("plastics", node_label="PolymerFamily")
    assert from_spec is not None
    assert from_spec.source == GRAPH_SYNC_SOURCE_DOMAIN_SPEC
    assert (from_spec.entity_type, from_spec.id_property) == ("polymerfamilies", "code")

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
        await router.notify_graph_sync(
            tenant_id="tenant-acme", entity_id="ent-9", fields={}, domain="metals"
        )
    payload = mock_exec.await_args.kwargs["payload"]
    assert payload["entity_type"] == "facilities"
    assert payload["batch"][0]["facility_id"] == "ent-9"


def test_get_router_reads_specs_from_settings_domains_dir(tmp_path):
    """The singleton resolves contracts from the same domains/ the converge path reads."""

    class SettingsStub:
        gate_url = "https://gate:8080"
        domains_dir = str(_domains_dir(tmp_path, {"plastics": _PLASTICS_SPEC}))
        graph_sync_entity_type = ""
        graph_sync_id_property = ""

    original = packet_router_module._router_singleton
    packet_router_module._router_singleton = None
    try:
        router = get_router(SettingsStub())
        contract = router.graph_sync_contract("plastics")
        assert contract is not None
        assert contract.entity_type == "facilities"
        assert contract.source == GRAPH_SYNC_SOURCE_DOMAIN_SPEC
        # Empty fallback pair: a domain without a spec endpoint fails closed.
        assert router.graph_sync_contract("metals") is None
    finally:
        packet_router_module._router_singleton = original


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
