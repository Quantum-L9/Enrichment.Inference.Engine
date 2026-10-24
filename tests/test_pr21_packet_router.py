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


@pytest.fixture
def plastics_router(tmp_path: Path) -> PacketRouter:
    """A router whose `plastics` domain spec declares CEG's `facilities` endpoint.

    GRAPH-IDENT-02: a router with no reader and no fallback has no sync contract
    and sends nothing, so every graph-sync test states where its contract comes
    from.
    """
    reader = DomainYamlReader(_domains_dir(tmp_path, {"plastics": _PLASTICS_SPEC}))
    return PacketRouter(gate_url="https://gate-node:8080", domain_reader=reader)


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
async def test_notify_graph_sync_sends_ceg_sync_contract_to_gate(plastics_router):
    """GAP-6 successor: graph sync is CEG's `sync` action, sent through Gate.

    GRAPH-IDENT-02: the endpoint (`facilities`) and its id property
    (`facility_id`) come from the `plastics` domain's own spec, not a constant.
    """
    router = plastics_router

    response_packet = create_transport_packet(
        action="sync",
        payload={"status": "success", "entity_type": "facilities", "synced_count": 1},
        tenant="plastics",
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )

    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=response_packet
    ) as mock_exec:
        result = await router.notify_graph_sync(
            tenant_id="plastics",
            entity_id="ent-001",
            fields={"material_type": "HDPE", "mfi_range": "2-4"},
            domain="plastics",
        )
        assert result["status"] == "success"
        assert "packet_id" in result
        kwargs = mock_exec.await_args.kwargs
    assert kwargs["action"] == "sync"
    assert kwargs["tenant"] == "plastics"
    assert kwargs["payload"]["entity_type"] == "facilities"
    assert kwargs["payload"]["batch"][0]["facility_id"] == "ent-001"
    assert kwargs["payload"]["batch"][0]["domain"] == "plastics"
    assert kwargs["idempotency_key"].startswith("eie:sync:plastics:ent-001:")
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
    # A label no endpoint targets among several is ambiguous: the ontology rule
    # applies only when no label was supplied (F-248-4).
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
async def test_graph_sync_fails_closed_for_a_domain_without_a_contract(plastics_router):
    """No spec endpoint and no fallback: nothing is sent, the skip is reported."""
    router = plastics_router
    with patch.object(router._client, "execute", new_callable=AsyncMock) as mock_exec:
        result = await router.notify_graph_sync(
            tenant_id="tenant-acme",
            entity_id="ent-001",
            fields={"material_type": "HDPE"},
            domain="metals",  # no such spec
        )
    assert result is None, "None means not synced (RV-DOMAIN-RD-07), here because nothing was sent"
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

    ok = {"status": "success", "entity_type": "facilities", "synced_count": 1}
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_ceg_response(ok)
    ) as mock_exec:
        result = await router.notify_graph_sync(
            tenant_id="tenant-acme", entity_id="ent-9", fields={}, domain="metals"
        )
    payload = mock_exec.await_args.kwargs["payload"]
    assert payload["entity_type"] == "facilities"
    assert payload["batch"][0]["facility_id"] == "ent-9"
    assert result is not None
    assert result["synced_count"] == 1


@pytest.mark.asyncio
async def test_acknowledgment_is_checked_against_the_domains_own_endpoint(tmp_path):
    """The ack must name the endpoint the domain spec resolved, not a global default."""
    reader = DomainYamlReader(_domains_dir(tmp_path, {"plastics": _PLASTICS_SPEC}))
    router = PacketRouter(gate_url="https://gate-node:8080", domain_reader=reader)
    acked_for_other_endpoint = {"status": "success", "entity_type": "facilities", "synced_count": 1}
    with patch.object(
        router._client,
        "execute",
        new_callable=AsyncMock,
        return_value=_ceg_response(acked_for_other_endpoint),
    ) as mock_exec:
        result = await router.notify_graph_sync(
            tenant_id="plastics",
            entity_id="pf-1",
            fields={},
            domain="plastics",
            node_label="PolymerFamily",
        )
    assert mock_exec.await_args.kwargs["payload"]["entity_type"] == "polymerfamilies"
    assert mock_exec.await_args.kwargs["payload"]["batch"][0]["code"] == "pf-1"
    assert result is None, "a `facilities` ack does not confirm a `polymerfamilies` sync"


def test_get_router_reads_specs_from_settings_domains_dir(tmp_path, monkeypatch):
    """The singleton resolves contracts from the same domains/ the converge path reads."""

    class SettingsStub:
        gate_url = "https://gate:8080"
        domains_dir = str(_domains_dir(tmp_path, {"plastics": _PLASTICS_SPEC}))
        graph_sync_entity_type = ""
        graph_sync_id_property = ""

    monkeypatch.setattr("app.engines.packet_router._router_singleton", None)
    router = get_router(SettingsStub())
    contract = router.graph_sync_contract("plastics")
    assert contract is not None
    assert contract.entity_type == "facilities"
    assert contract.source == GRAPH_SYNC_SOURCE_DOMAIN_SPEC
    # Empty fallback pair: a domain without a spec endpoint fails closed.
    assert router.graph_sync_contract("metals") is None


def test_unmatched_label_still_selects_a_single_declared_endpoint():
    """`object_type="Account"` is a CRM object, not a node label; one endpoint is still it."""
    spec = {
        "sync": {
            "endpoints": [
                {"path": "/v1/sync/metals", "targetnode": "Metal", "idproperty": "metal_id"}
            ]
        }
    }
    contract = resolve_graph_sync_contract(spec, node_label="Account")
    assert contract is not None
    assert (contract.entity_type, contract.id_property) == ("metals", "metal_id")


@pytest.mark.asyncio
async def test_ambiguous_declared_endpoints_are_skipped_not_defaulted(tmp_path):
    """Several endpoints, no label: nothing is sent and the fallback is NOT used.

    The production caller (SideEffectCoordinator) used to call without a node
    label; the resolver returned None and the `facilities` fallback silently
    took over for an entity of another type.
    """
    ambiguous = {
        "name": "mixed",
        "ontology": {"nodes": [{"label": "A"}, {"label": "B"}]},
        "sync": {
            "endpoints": [
                {"path": "/v1/sync/a", "targetnode": "A", "idproperty": "a_id"},
                {"path": "/v1/sync/b", "targetnode": "B", "idproperty": "b_id"},
            ]
        },
    }
    reader = DomainYamlReader(_domains_dir(tmp_path, {"mixed": ambiguous}))
    router = PacketRouter(
        gate_url="https://gate-node:8080",
        domain_reader=reader,
        fallback_entity_type="facilities",
        fallback_id_property="facility_id",
    )
    assert router.graph_sync_contract("mixed") is None
    with patch.object(router._client, "execute", new_callable=AsyncMock) as mock_exec:
        result = await router.notify_graph_sync(
            tenant_id="mixed", entity_id="x-1", fields={}, domain="mixed"
        )
    assert result is None
    mock_exec.assert_not_awaited()
    # The label resolves it; the fallback was never the answer.
    chosen = router.graph_sync_contract("mixed", node_label="B")
    assert chosen is not None
    assert (chosen.entity_type, chosen.id_property, chosen.source) == (
        "b",
        "b_id",
        GRAPH_SYNC_SOURCE_DOMAIN_SPEC,
    )


@pytest.mark.parametrize(
    ("filename_body", "case"),
    [("", "empty-file"), ("- just\n- a list\n", "list-root"), ("ontology: [:::", "malformed")],
)
def test_unparseable_spec_counts_as_declaring_no_endpoint(tmp_path, filename_body, case):
    """An empty, list-rooted or malformed spec is "declares none": fallback, else fail closed."""
    root = tmp_path / "domains"
    (root / "odd").mkdir(parents=True)
    (root / "odd" / "spec.yaml").write_text(filename_body, encoding="utf-8")
    reader = DomainYamlReader(root)

    closed = PacketRouter(gate_url="https://gate-node:8080", domain_reader=reader)
    assert closed.graph_sync_contract("odd") is None, case

    with_fallback = PacketRouter(
        gate_url="https://gate-node:8080",
        domain_reader=reader,
        fallback_entity_type="facilities",
        fallback_id_property="facility_id",
    )
    contract = with_fallback.graph_sync_contract("odd")
    assert contract is not None, case
    assert contract.source == GRAPH_SYNC_SOURCE_SETTINGS_FALLBACK


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
async def test_notify_graph_sync_returns_none_on_worker_failure_packet(plastics_router):
    """Reproduction of RV-DOMAIN-RD-07 at the router seam.

    Before the repair the failure payload ``{"status": "failed", ...}`` came back
    as a truthy dict and the coordinator recorded the row as synced.
    """
    router = plastics_router
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_ceg_failure_packet()
    ) as mock_exec:
        result = await router.notify_graph_sync(
            tenant_id="plastics", entity_id="ent-001", fields={"x": 1}, domain="plastics"
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
        {"status": "success", "entity_type": "facilities", "synced_count": 2},
        {"status": "success", "entity_type": "outcome", "synced_count": 1},
        {"status": "success", "entity_type": "facilities"},
        {"status": "failed", "code": "execution_failed"},
        {},
    ],
    ids=[
        "all-rejected",
        "zero-synced",
        "count-not-this-batch",
        "other-endpoint",
        "no-count",
        "failed-status",
        "empty",
    ],
)
async def test_notify_graph_sync_returns_none_when_ceg_does_not_confirm_the_operation(
    payload, plastics_router
):
    router = plastics_router
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_ceg_response(payload)
    ):
        result = await router.notify_graph_sync(
            tenant_id="plastics", entity_id="ent-001", fields={}, domain="plastics"
        )
    assert result is None


@pytest.mark.asyncio
async def test_notify_graph_sync_returns_the_confirmed_acknowledgment(plastics_router):
    router = plastics_router
    ok = {"status": "success", "entity_type": "facilities", "synced_count": 1}
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_ceg_response(ok)
    ):
        result = await router.notify_graph_sync(
            tenant_id="plastics", entity_id="ent-001", fields={}, domain="plastics"
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
    assert ok(
        {"status": "success", "entity_type": "facilities", "synced_count": 1}, expected_count=1
    )
    assert not ok(
        {"status": "success", "entity_type": "facilities", "synced_count": 3}, expected_count=1
    ), "synced_count must equal the batch the caller sent"
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
