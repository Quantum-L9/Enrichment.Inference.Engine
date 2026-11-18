"""Graph-sync contract (docs/contracts/dependencies/constellation-gate.yaml).

GRAPH-IDENT-02 on top of RV-DOMAIN-RD-07: the CEG `sync` endpoint a tenant's
rows are addressed to is resolved from the tenant's own spec (tenant id ==
domain spec id, the spec CEG loads to validate the sync), never from the
payload's `domain`; the acknowledgment is checked against the endpoint that was
resolved, an ambiguous declaration (including an explicit node label no
endpoint targets) is refused rather than defaulted, an id the reader's tenant
grammar refuses never reaches the filesystem, the labeled settings fallback
applies only to a spec that declares no endpoint and is empty by default, and
with both fallback values empty such a tenant fails closed.

Nothing here needs the provider runtime: `app.engines.packet_router` imports the
Gate SDK and the domain reader only, and the coordinator pin patches its
collaborators like tests/unit/test_side_effect_coordinator.py does. The one
exception is the coordinator pin itself: `commit_after_enrich` imports
`app.services.result_store`, which reaches SQLAlchemy through `pg_models`, so
that test runs only where the DB runtime is installed (the L9 Constitution Gate
installs neither SQLAlchemy nor the provider SDK).

L9_META:
  tier: 2
  domain: graph-sync
  authority: operator decision 2026-10-22 (plan dac62dc3); CEG SyncEndpointSpec
  pr_class: app_code + tier2_test
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from constellation_node_sdk.transport import create_transport_packet

from app.core.config import Settings
from app.engines.domain_yaml_reader import DomainYamlReader
from app.engines.packet_router import (
    GRAPH_SYNC_SOURCE_DOMAIN_SPEC,
    GRAPH_SYNC_SOURCE_SETTINGS_FALLBACK,
    PacketRouter,
    resolve_graph_sync_contract,
)
from app.services.side_effect_coordinator import SideEffectCoordinator

GATE = "https://gate-node:8080"

_HAS_DB_RUNTIME = importlib.util.find_spec("sqlalchemy") is not None
requires_db_runtime = pytest.mark.skipif(
    not _HAS_DB_RUNTIME,
    reason="commit_after_enrich imports app.services.result_store (SQLAlchemy via pg_models); "
    "the L9 Constitution Gate installs no DB runtime",
)

# CEG's owner-native SyncEndpointSpec shape (Cognitive.Engine.Graphs
# engine/config/schema.py): path / targetnode / idproperty; CEG keys the sync
# handler on the path's last segment.
PLASTICS_SPEC: dict[str, Any] = {
    "name": "plastics",
    "version": "0.3.0",
    # Typed row contracts (owner decision 2026-11-11): each endpoint's target
    # node declares the row it accepts; packet_router builds rows from it.
    "ontology": {
        "nodes": [{"label": "Facility", "properties": [{"name": "facility_id", "type": "string"}]}]
    },
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

# A spec planted OUTSIDE the domains root: `domains/../outside.yaml`. A
# payload-controlled id of "../outside" would reach it through the flat-file
# branch of DomainYamlReader.load if the id were not validated first.
OUTSIDE_SPEC: dict[str, Any] = {
    "name": "outside",
    "ontology": {"nodes": [{"label": "Loot"}]},
    "sync": {
        "endpoints": [{"path": "/v1/sync/attacker", "targetnode": "Loot", "idproperty": "pwn"}]
    },
}

ALPHA_SPEC: dict[str, Any] = {
    "name": "alpha",
    "ontology": {"nodes": [{"label": "Site"}]},
    "sync": {
        "endpoints": [{"path": "/v1/sync/sites", "targetnode": "Site", "idproperty": "site_id"}]
    },
}

BETA_SPEC: dict[str, Any] = {
    "name": "beta",
    "ontology": {
        "nodes": [{"label": "Facility", "properties": [{"name": "facility_id", "type": "string"}]}]
    },
    "sync": {
        "endpoints": [
            {"path": "/v1/sync/facilities", "targetnode": "Facility", "idproperty": "facility_id"}
        ]
    },
}

# EIE's shipped specs, the directory Settings.domains_dir names by default.
REPO_DOMAINS = Path(__file__).resolve().parents[2] / "domains"


def _reader(tmp_path: Path, specs: dict[str, dict[str, Any]]) -> DomainYamlReader:
    root = tmp_path / "domains"
    for tenant, spec in specs.items():
        (root / tenant).mkdir(parents=True)
        (root / tenant / "spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    return DomainYamlReader(root)


def _outside_reader(tmp_path: Path) -> DomainYamlReader:
    root = tmp_path / "domains"
    root.mkdir()
    (tmp_path / "outside.yaml").write_text(yaml.safe_dump(OUTSIDE_SPEC), encoding="utf-8")
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
        assert contract is not None
        assert contract.entity_type == "metals"

    assert resolve_graph_sync_contract(AMBIGUOUS_SPEC) is None
    assert resolve_graph_sync_contract(NO_SYNC_SPEC) is None


# ── Acknowledgment is checked against the resolved endpoint ──────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ack_is_checked_against_the_domains_resolved_endpoint(specs_reader) -> None:
    router = PacketRouter(gate_url=GATE, domain_reader=specs_reader)
    other = {"status": "success", "entity_type": "polymerfamilies", "synced_count": 1}
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_ack(other)
    ) as execute:
        result = await router.notify_graph_sync(
            tenant_id="plastics",
            entity_id="fac-1",
            fields={},
            domain="plastics",
            node_label="Facility",
        )
    sent = execute.await_args.kwargs["payload"]
    assert sent["entity_type"] == "facilities"
    assert sent["batch"][0]["facility_id"] == "fac-1"
    assert result is None, "an ack for another endpoint does not confirm this sync"

    matching = {"status": "success", "entity_type": "facilities", "synced_count": 1}
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_ack(matching)
    ):
        confirmed = await router.notify_graph_sync(
            tenant_id="plastics",
            entity_id="fac-1",
            fields={},
            domain="plastics",
            node_label="Facility",
        )
    assert confirmed is not None
    assert confirmed["synced_count"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_endpoint_whose_target_node_the_spec_does_not_declare_sends_nothing(
    specs_reader,
) -> None:
    """PolymerFamily has an endpoint but no declared node: no typed row can be built."""
    router = PacketRouter(gate_url=GATE, domain_reader=specs_reader)
    contract = router.graph_sync_contract("plastics", node_label="PolymerFamily")
    assert contract is not None
    assert (contract.entity_type, contract.row_schema) == ("polymerfamilies", None)
    with patch.object(router._client, "execute", new_callable=AsyncMock) as execute:
        result = await router.notify_graph_sync(
            tenant_id="plastics",
            entity_id="pf-1",
            fields={},
            domain="plastics",
            node_label="PolymerFamily",
        )
    assert result is None
    execute.assert_not_awaited()


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
            tenant_id="mixed", entity_id="x-1", fields={}, domain="mixed"
        )
    assert result is None
    execute.assert_not_awaited()


# ── No declared endpoint: labeled fallback, else fail closed ─────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_declared_endpoint_resolves_the_labeled_fallback_but_sends_no_untyped_row(
    specs_reader,
) -> None:
    """The fallback names an endpoint; it declares no row, so nothing typed can be sent.

    Typed rows (owner decision 2026-11-11): the row schema an endpoint accepts
    is the tenant spec's declared types. A tenant whose spec declares no
    endpoint declares no row either, so the settings fallback still resolves
    (and is logged as such) but every row is refused before sending.
    """
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
        assert contract.row_schema is None

    with patch.object(router._client, "execute", new_callable=AsyncMock) as execute:
        result = await router.notify_graph_sync(
            tenant_id="plain", entity_id="t-1", fields={"name": "x"}, domain="plain"
        )
    assert result is None
    execute.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_both_fallback_values_empty_fails_closed(specs_reader) -> None:
    router = PacketRouter(gate_url=GATE, domain_reader=specs_reader)
    for domain in ("plain", "unknown-domain", "mixed"):
        assert router.graph_sync_contract(domain) is None, domain
    with patch.object(router._client, "execute", new_callable=AsyncMock) as execute:
        result = await router.notify_graph_sync(
            tenant_id="plain", entity_id="t-1", fields={}, domain="plain"
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
    assert fallback is not None
    assert fallback.source == GRAPH_SYNC_SOURCE_SETTINGS_FALLBACK


# ── F-248-1: an illegal id never reaches the filesystem ──────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("domain_id", ["../outside", "..", "/abs/outside", "abc/../../outside"])
def test_reader_refuses_an_id_outside_the_tenant_grammar(tmp_path: Path, domain_id: str) -> None:
    reader = _outside_reader(tmp_path)
    with pytest.raises(ValueError, match="Invalid domain id"):
        reader.load(domain_id)


@pytest.mark.unit
def test_traversal_id_takes_the_labeled_fallback_never_the_outside_spec(tmp_path: Path) -> None:
    router = PacketRouter(
        gate_url=GATE,
        domain_reader=_outside_reader(tmp_path),
        fallback_entity_type="facilities",
        fallback_id_property="facility_id",
    )
    contract = router.graph_sync_contract("../outside")
    assert contract is not None
    assert contract.source == GRAPH_SYNC_SOURCE_SETTINGS_FALLBACK
    assert (contract.entity_type, contract.id_property) == ("facilities", "facility_id")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_traversal_tenant_sends_nothing_without_a_fallback(tmp_path: Path) -> None:
    router = PacketRouter(gate_url=GATE, domain_reader=_outside_reader(tmp_path))
    assert router.graph_sync_contract("../outside") is None
    with patch.object(router._client, "execute", new_callable=AsyncMock) as execute:
        result = await router.notify_graph_sync(
            tenant_id="../outside", entity_id="x-1", fields={}, domain="../outside"
        )
    assert result is None
    execute.assert_not_awaited()


# ── F-248-2: the tenant's spec governs, not the payload domain ───────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_contract_is_the_tenants_not_the_payload_domains(tmp_path: Path) -> None:
    router = PacketRouter(
        gate_url=GATE, domain_reader=_reader(tmp_path, {"alpha": ALPHA_SPEC, "beta": BETA_SPEC})
    )
    ok = {"status": "success", "entity_type": "facilities", "synced_count": 1}
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_ack(ok)
    ) as execute:
        result = await router.notify_graph_sync(
            tenant_id="beta", entity_id="f-1", fields={}, domain="alpha"
        )
    sent = execute.await_args.kwargs["payload"]
    assert sent["entity_type"] == "facilities"
    assert sent["batch"][0]["facility_id"] == "f-1"
    assert result is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_tenant_without_a_spec_is_not_synced_by_another_domains_spec(
    tmp_path: Path,
) -> None:
    router = PacketRouter(gate_url=GATE, domain_reader=_reader(tmp_path, {"alpha": ALPHA_SPEC}))
    with patch.object(router._client, "execute", new_callable=AsyncMock) as execute:
        result = await router.notify_graph_sync(
            tenant_id="gamma", entity_id="s-1", fields={}, domain="alpha"
        )
    assert result is None
    execute.assert_not_awaited()


# ── F-248-3: the shipped spec declares CEG's endpoints; no global default ────


@pytest.mark.unit
@pytest.mark.parametrize("node_label", [None, "Facility", "Account"])
def test_shipped_plasticos_spec_declares_the_facilities_endpoint_with_its_typed_row(
    node_label: str | None,
) -> None:
    """EIE enriches Facility only, so its spec declares CEG's facilities endpoint alone.

    A single declared endpoint is the tenant's sync target whatever the
    caller's label (an Odoo/CRM object type is not a node label). The contract
    carries the typed row the spec declares, childsync field included.
    """
    router = PacketRouter(gate_url=GATE, domain_reader=DomainYamlReader(REPO_DOMAINS))
    contract = router.graph_sync_contract("plasticos", node_label=node_label)
    assert contract is not None
    assert contract.source == GRAPH_SYNC_SOURCE_DOMAIN_SPEC
    assert (contract.entity_type, contract.id_property) == ("facilities", "facility_id")
    assert contract.row_schema is not None
    assert contract.row_schema["processed_polymers"]["items"]["type"] == "map"
    assert "city" not in contract.row_schema  # evidenceonly: never on a row


@pytest.mark.unit
def test_settings_fallback_pair_defaults_to_empty() -> None:
    fields = Settings.model_fields
    assert fields["graph_sync_entity_type"].default == ""
    assert fields["graph_sync_id_property"].default == ""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_settings_send_nothing_for_a_tenant_without_a_spec_endpoint(
    specs_reader,
) -> None:
    fields = Settings.model_fields
    router = PacketRouter(
        gate_url=GATE,
        domain_reader=specs_reader,
        fallback_entity_type=fields["graph_sync_entity_type"].default,
        fallback_id_property=fields["graph_sync_id_property"].default,
    )
    with patch.object(router._client, "execute", new_callable=AsyncMock) as execute:
        result = await router.notify_graph_sync(
            tenant_id="plain", entity_id="t-1", fields={}, domain="plain"
        )
    assert result is None
    execute.assert_not_awaited()


# ── F-248-4: an explicit label no endpoint targets is ambiguous ──────────────


@pytest.mark.unit
def test_unmatched_explicit_label_among_several_endpoints_is_ambiguous() -> None:
    assert resolve_graph_sync_contract(PLASTICS_SPEC, node_label="Account") is None


@pytest.mark.unit
def test_unmatched_explicit_label_does_not_reach_the_fallback(specs_reader) -> None:
    router = PacketRouter(
        gate_url=GATE,
        domain_reader=specs_reader,
        fallback_entity_type="facilities",
        fallback_id_property="facility_id",
    )
    assert router.graph_sync_contract("plastics", node_label="Account") is None


# ── The node label reaches the router through the coordinator ────────────────


@requires_db_runtime
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
    assert report.score_invalidated is False
    assert router.notify_score_invalidate.await_count == 0
    assert router.notify_graph_sync.await_args.kwargs["node_label"] == "Facility"


@pytest.mark.unit
def test_packet_router_does_not_dispatch_score_invalidate() -> None:
    """score-invalidate has no Gate owner, so the router exposes no sender for it."""
    router = PacketRouter(gate_url=GATE)
    assert not hasattr(router, "notify_score_invalidate")
    score_methods = [name for name in dir(PacketRouter) if "score" in name]
    assert score_methods == []
