"""Graph-sync contract (docs/contracts/dependencies/constellation-gate.yaml).

GRAPH-IDENT-02 on top of RV-DOMAIN-RD-07: the CEG `sync` endpoint a tenant's
rows are addressed to is resolved per domain from that domain's own spec, the
acknowledgment is checked against the endpoint that was resolved, an ambiguous
declaration is refused rather than defaulted, the labeled settings fallback
applies only to a spec that declares no endpoint, and with both fallback values
empty such a domain fails closed.

Nothing here needs the provider runtime: `app.engines.packet_router` imports the
Gate SDK and the domain reader only, and the coordinator pin patches its
collaborators like tests/unit/test_side_effect_coordinator.py does.

L9_META:
  tier: 2
  domain: graph-sync
  authority: operator decision 2026-10-22 (plan dac62dc3); CEG SyncEndpointSpec
  pr_class: app_code + tier2_test
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from constellation_node_sdk.transport import create_transport_packet

from app.engines.domain_yaml_reader import DomainYamlReader
from app.engines.packet_router import (
    GRAPH_SYNC_SOURCE_DOMAIN_SPEC,
    GRAPH_SYNC_SOURCE_SETTINGS_FALLBACK,
    PacketRouter,
    resolve_graph_sync_contract,
)
from app.services.side_effect_coordinator import SideEffectCoordinator

GATE = "https://gate-node:8080"

# CEG's owner-native SyncEndpointSpec shape (Cognitive.Engine.Graphs
# engine/config/schema.py): path / targetnode / idproperty; CEG keys the sync
# handler on the path's last segment.
PLASTICS_SPEC: dict[str, Any] = {
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

AMBIGUOUS_SPEC: dict[str, Any] = {
    "name": "mixed",
    "ontology": {"nodes": [{"label": "A"}, {"label": "B"}]},
    "sync": {
        "endpoints": [
            {"path": "/v1/sync/a", "targetnode": "A", "idproperty": "a_id"},
            {"path": "/v1/sync/b", "targetnode": "B", "idproperty": "b_id"},
        ]
    },
}

NO_SYNC_SPEC: dict[str, Any] = {"name": "plain", "ontology": {"nodes": [{"label": "Thing"}]}}


def _reader(tmp_path: Path, specs: dict[str, dict[str, Any]]) -> DomainYamlReader:
    root = tmp_path / "domains"
    for tenant, spec in specs.items():
        (root / tenant).mkdir(parents=True)
        (root / tenant / "spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    return DomainYamlReader(root)


def _ack(payload: dict[str, Any]) -> object:
    return create_transport_packet(
        action="sync",
        payload=payload,
        tenant="tenant-acme",
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )


@pytest.fixture
def specs_reader(tmp_path: Path) -> DomainYamlReader:
    return _reader(
        tmp_path, {"plastics": PLASTICS_SPEC, "mixed": AMBIGUOUS_SPEC, "plain": NO_SYNC_SPEC}
    )


# ── Resolution rule ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_resolution_order_label_then_single_then_ontology() -> None:
    by_ontology = resolve_graph_sync_contract(PLASTICS_SPEC)
    assert by_ontology is not None
    assert (by_ontology.entity_type, by_ontology.id_property) == ("facilities", "facility_id")
    assert by_ontology.source == GRAPH_SYNC_SOURCE_DOMAIN_SPEC

    by_label = resolve_graph_sync_contract(PLASTICS_SPEC, node_label="PolymerFamily")
    assert by_label is not None
    assert (by_label.entity_type, by_label.id_property) == ("polymerfamilies", "code")

    single = {"sync": {"endpoints": [{"path": "/v1/sync/metals", "idproperty": "metal_id"}]}}
    for label in (None, "Account"):
        contract = resolve_graph_sync_contract(single, node_label=label)
        assert contract is not None and contract.entity_type == "metals"

    assert resolve_graph_sync_contract(AMBIGUOUS_SPEC) is None
    assert resolve_graph_sync_contract(NO_SYNC_SPEC) is None


# ── Acknowledgment is checked against the resolved endpoint ──────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ack_is_checked_against_the_domains_resolved_endpoint(specs_reader) -> None:
    router = PacketRouter(gate_url=GATE, domain_reader=specs_reader)
    other = {"status": "success", "entity_type": "facilities", "synced_count": 1}
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_ack(other)
    ) as execute:
        result = await router.notify_graph_sync(
            tenant_id="tenant-acme",
            entity_id="pf-1",
            fields={},
            domain="plastics",
            node_label="PolymerFamily",
        )
    sent = execute.await_args.kwargs["payload"]
    assert sent["entity_type"] == "polymerfamilies"
    assert sent["batch"][0]["code"] == "pf-1"
    assert result is None, "an ack for another endpoint does not confirm this sync"

    matching = {"status": "success", "entity_type": "polymerfamilies", "synced_count": 1}
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_ack(matching)
    ):
        confirmed = await router.notify_graph_sync(
            tenant_id="tenant-acme",
            entity_id="pf-1",
            fields={},
            domain="plastics",
            node_label="PolymerFamily",
        )
    assert confirmed is not None and confirmed["synced_count"] == 1


# ── Ambiguous: skipped, never defaulted ──────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ambiguous_declaration_is_skipped_and_the_fallback_is_not_consulted(
    specs_reader,
) -> None:
    router = PacketRouter(
        gate_url=GATE,
        domain_reader=specs_reader,
        fallback_entity_type="facilities",
        fallback_id_property="facility_id",
    )
    assert router.graph_sync_contract("mixed") is None
    with patch.object(router._client, "execute", new_callable=AsyncMock) as execute:
        result = await router.notify_graph_sync(
            tenant_id="tenant-acme", entity_id="x-1", fields={}, domain="mixed"
        )
    assert result is None
    execute.assert_not_awaited()


# ── No declared endpoint: labeled fallback, else fail closed ─────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_declared_endpoint_uses_the_labeled_fallback(specs_reader) -> None:
    router = PacketRouter(
        gate_url=GATE,
        domain_reader=specs_reader,
        fallback_entity_type="facilities",
        fallback_id_property="facility_id",
    )
    for domain in ("plain", "unknown-domain"):
        contract = router.graph_sync_contract(domain)
        assert contract is not None, domain
        assert contract.source == GRAPH_SYNC_SOURCE_SETTINGS_FALLBACK
        assert (contract.entity_type, contract.id_property) == ("facilities", "facility_id")

    ok = {"status": "success", "entity_type": "facilities", "synced_count": 1}
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_ack(ok)
    ) as execute:
        result = await router.notify_graph_sync(
            tenant_id="tenant-acme", entity_id="t-1", fields={}, domain="plain"
        )
    assert execute.await_args.kwargs["payload"]["entity_type"] == "facilities"
    assert result is not None and result["status"] == "success"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_both_fallback_values_empty_fails_closed(specs_reader) -> None:
    router = PacketRouter(gate_url=GATE, domain_reader=specs_reader)
    for domain in ("plain", "unknown-domain", "mixed"):
        assert router.graph_sync_contract(domain) is None, domain
    with patch.object(router._client, "execute", new_callable=AsyncMock) as execute:
        result = await router.notify_graph_sync(
            tenant_id="tenant-acme", entity_id="t-1", fields={}, domain="plain"
        )
    assert result is None
    execute.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize("body", ["", "- just\n- a list\n", "ontology: [:::"])
def test_unparseable_spec_is_declares_none(tmp_path: Path, body: str) -> None:
    root = tmp_path / "domains"
    (root / "odd").mkdir(parents=True)
    (root / "odd" / "spec.yaml").write_text(body, encoding="utf-8")
    reader = DomainYamlReader(root)
    assert PacketRouter(gate_url=GATE, domain_reader=reader).graph_sync_contract("odd") is None
    fallback = PacketRouter(
        gate_url=GATE,
        domain_reader=reader,
        fallback_entity_type="facilities",
        fallback_id_property="facility_id",
    ).graph_sync_contract("odd")
    assert fallback is not None and fallback.source == GRAPH_SYNC_SOURCE_SETTINGS_FALLBACK


# ── The node label reaches the router through the coordinator ────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_coordinator_forwards_the_node_label_to_the_router() -> None:
    router = MagicMock()
    router.notify_graph_sync = AsyncMock(
        return_value={"status": "success", "entity_type": "facilities", "synced_count": 1}
    )
    router.notify_score_invalidate = AsyncMock()
    emitter = MagicMock()
    emitter.emit_enrichment_completed = AsyncMock()
    with (
        patch("app.models.schemas.EnrichResponse.model_validate", return_value=MagicMock()),
        patch("app.services.result_store.ResultStore") as result_store_cls,
        patch("app.engines.packet_router.get_router", return_value=router),
        patch("app.services.event_emitter.get_emitter", return_value=emitter),
    ):
        result_store_cls.return_value.persist_enrich_response = AsyncMock()
        report = await SideEffectCoordinator().commit_after_enrich(
            tenant="plasticos",
            entity_id="e1",
            object_type="Account",
            domain="plasticos",
            response_dict={"state": "completed", "fields": {"x": 1}, "confidence": 0.9},
            settings=SimpleNamespace(gate_url=GATE),
            node_label="Facility",
        )
    assert report.graph_synced is True
    assert router.notify_graph_sync.await_args.kwargs["node_label"] == "Facility"
