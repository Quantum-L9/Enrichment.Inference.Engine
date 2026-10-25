"""
tests/unit/test_observed_relationship_producer.py

QM.POC0.EIE.OBSERVED_RELATIONSHIP_PRODUCER.003 — one observed HubSpot
association becomes one RelationshipAssertion over the existing
``PacketRouter.notify_relationship_sync``.

Every HubSpot response here is SYNTHETIC (a patched ``httpx.get``), shaped as
the documented ``GET /crm/v4/objects/{from}/{id}/associations/{to}`` answer;
none is a recorded or live source. Gate is a patched ``execute`` on the
router's one Gate client. The two tenant vocabularies are unrelated synthetic
graphs; neither names a product domain.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml
from constellation_node_sdk.gate import GateConnectionError
from constellation_node_sdk.transport import create_transport_packet

from app.engines.domain_yaml_reader import DomainYamlReader
from app.engines.packet_router import PacketRouter
from app.services.crm import hubspot_client as hubspot_module
from app.services.crm import observed_relationship_producer as producer_module
from app.services.crm.base import CRMCredentials, CRMType
from app.services.crm.hubspot_client import HubSpotAssociationReadError, HubSpotClient
from app.services.crm.observed_relationship_producer import (
    ObservedAssociationBinding,
    ProducerStatus,
    produce_observed_relationship,
)

pytestmark = pytest.mark.unit


def _spec(
    domain_id: str, subject: str, relation: str, obj: str, entity_type: str
) -> dict[str, Any]:
    return {
        "domain": {"id": domain_id, "name": "synthetic", "version": "0.1.0"},
        "ontology": {
            "nodes": [
                {"label": subject, "properties": [{"name": "subject_id", "required": True}]},
                {"label": obj, "properties": [{"name": "object_id", "required": True}]},
            ],
            "edges": [
                {
                    "type": relation,
                    "from": subject,
                    "to": obj,
                    "direction": "DIRECTED",
                    "managedby": "sync",
                }
            ],
        },
        "sync": {
            "endpoints": [
                {
                    "path": f"/v1/sync/{entity_type}",
                    "targetedge": relation,
                    "fromnode": subject,
                    "tonode": obj,
                    "batchstrategy": "UNWINDMERGE",
                }
            ]
        },
    }


# tenant -> (subject label, relation, object label, sync entity type, provider pair, typeId)
VOCABULARIES: dict[str, tuple[str, str, str, str, tuple[str, str], int]] = {
    "orbit-yard": (
        "OrbitalDepot",
        "STOCKS",
        "PropellantGrade",
        "stocks",
        ("companies", "deals"),
        342,
    ),
    "cave-works": ("AgingCave", "TENDED_BY", "Affineur", "tended-by", ("deals", "contacts"), 3),
}
FROM_ID = "1001"
TO_ID = "2002"


def _binding(tenant: str, **overrides: Any) -> ObservedAssociationBinding:
    subject, relation, obj, _entity, (from_type, to_type), type_id = VOCABULARIES[tenant]
    fields: dict[str, Any] = {
        "from_object_type": from_type,
        "to_object_type": to_type,
        "association_category": "HUBSPOT_DEFINED",
        "association_type_id": type_id,
        "subject_label": subject,
        "relation_type": relation,
        "object_label": obj,
    }
    fields.update(overrides)
    return ObservedAssociationBinding(**fields)


def _associations_body(tenant: str, *, to_id: str = TO_ID, **extra: Any) -> dict[str, Any]:
    type_id = VOCABULARIES[tenant][5]
    return {
        "results": [
            {
                "toObjectId": int(to_id),
                "associationTypes": [
                    {"category": "HUBSPOT_DEFINED", "typeId": type_id, "label": None},
                    {"category": "USER_DEFINED", "typeId": 99, "label": "Synthetic label"},
                ],
            }
        ],
        **extra,
    }


def _http(status: int = 200, body: Any = None) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status
    response.headers = {}
    response.json.return_value = body
    return response


@pytest.fixture
def router(tmp_path: Path) -> PacketRouter:
    root = tmp_path / "domains"
    for tenant, (subject, relation, obj, entity_type, *_rest) in VOCABULARIES.items():
        (root / tenant).mkdir(parents=True)
        spec = _spec(tenant, subject, relation, obj, entity_type)
        (root / tenant / "spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    return PacketRouter(gate_url="https://gate-node:8080", domain_reader=DomainYamlReader(root))


@pytest.fixture
def hubspot() -> HubSpotClient:
    return HubSpotClient(
        CRMCredentials(crm_type=CRMType.HUBSPOT, credentials={"access_token": "t"})
    )


def _ack(tenant: str, entity_type: str, created: int = 1, replayed: int = 0) -> Any:
    payload = {
        "status": "success",
        "entity_type": entity_type,
        "synced_count": 1,
        "created_count": created,
        "replayed_count": replayed,
    }
    return create_transport_packet(
        action="sync",
        payload=payload,
        tenant=tenant,
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )


async def _produce(
    router: PacketRouter,
    hubspot: HubSpotClient,
    tenant: str,
    *,
    http: Any,
    gate: Any = None,
    binding: ObservedAssociationBinding | None = None,
    from_id: str = FROM_ID,
    to_id: str = TO_ID,
) -> tuple[Any, MagicMock, AsyncMock]:
    entity_type = VOCABULARIES[tenant][3]
    get = (
        MagicMock(side_effect=http) if isinstance(http, Exception) else MagicMock(return_value=http)
    )
    execute = AsyncMock(
        side_effect=gate if isinstance(gate, Exception) else None,
        return_value=gate if gate is not None else _ack(tenant, entity_type),
    )
    with (
        patch.object(hubspot_module.httpx, "get", get),
        patch.object(router._client, "execute", execute),
    ):
        result = await produce_observed_relationship(
            tenant_id=tenant,
            binding=binding or _binding(tenant),
            from_record_id=from_id,
            to_record_id=to_id,
            hubspot=hubspot,
            router=router,
        )
    return result, get, execute


# ── HubSpot association read (read-only, fail closed) ─────────────────────────


def test_read_associations_returns_every_reported_type(hubspot: HubSpotClient) -> None:
    get = MagicMock(return_value=_http(body=_associations_body("orbit-yard")))
    with patch.object(hubspot_module.httpx, "get", get):
        associations = hubspot.read_associations("company", FROM_ID, "deal")
    url = get.call_args.args[0]
    assert url == f"https://api.hubapi.com/crm/v4/objects/companies/{FROM_ID}/associations/deals"
    assert get.call_args.kwargs["params"] == {"limit": 500}
    assert [(a.to_record_id, a.category, a.type_id) for a in associations] == [
        (TO_ID, "HUBSPOT_DEFINED", 342),
        (TO_ID, "USER_DEFINED", 99),
    ]
    assert associations[0].from_object_type == "companies"


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_any_non_200_answer_raises_never_empty(hubspot: HubSpotClient, status: int) -> None:
    with (
        patch.object(hubspot_module.httpx, "get", MagicMock(return_value=_http(status, {}))),
        pytest.raises(HubSpotAssociationReadError) as raised,
    ):
        hubspot.read_associations("companies", FROM_ID, "deals")
    assert raised.value.status_code == status


@pytest.mark.parametrize(
    "body",
    [
        None,
        {"results": "nope"},
        {"results": [{"associationTypes": [{"category": "HUBSPOT_DEFINED", "typeId": 1}]}]},
        {"results": [{"toObjectId": 5, "associationTypes": []}]},
        {"results": [{"toObjectId": 5, "associationTypes": [{"category": "HUBSPOT_DEFINED"}]}]},
        {"results": [{"toObjectId": 5, "associationTypes": [{"typeId": "1", "category": "X"}]}]},
        {"results": [], "paging": {"next": {"after": "abc"}}},
    ],
    ids=[
        "no-body",
        "results-not-list",
        "no-to-id",
        "no-types",
        "no-type-id",
        "type-id-text",
        "truncated",
    ],
)
def test_malformed_or_truncated_bodies_raise(hubspot: HubSpotClient, body: Any) -> None:
    with (
        patch.object(hubspot_module.httpx, "get", MagicMock(return_value=_http(body=body))),
        pytest.raises(HubSpotAssociationReadError),
    ):
        hubspot.read_associations("companies", FROM_ID, "deals")


def test_transport_failure_raises(hubspot: HubSpotClient) -> None:
    get = MagicMock(side_effect=httpx.ReadTimeout("slow"))
    with patch.object(hubspot_module.httpx, "get", get), pytest.raises(HubSpotAssociationReadError):
        hubspot.read_associations("companies", FROM_ID, "deals")


@pytest.mark.parametrize(
    ("from_type", "record_id"),
    [
        ("companies", "../deals"),
        ("companies", "12a"),
        ("companies/../x", FROM_ID),
        ("Deals", FROM_ID),
    ],
)
def test_ids_and_types_never_reshape_the_url(
    hubspot: HubSpotClient, from_type: str, record_id: str
) -> None:
    get = MagicMock()
    with patch.object(hubspot_module.httpx, "get", get), pytest.raises(HubSpotAssociationReadError):
        hubspot.read_associations(from_type, record_id, "deals")
    get.assert_not_called()


# ── Producer: observed association -> acknowledged relationship ───────────────


@pytest.mark.parametrize("tenant", sorted(VOCABULARIES))
async def test_an_observed_association_is_proposed_and_acknowledged(
    router: PacketRouter, hubspot: HubSpotClient, tenant: str
) -> None:
    result, get, execute = await _produce(
        router, hubspot, tenant, http=_http(body=_associations_body(tenant))
    )
    assert result.status is ProducerStatus.SYNCED
    assert result.ceg_ack is not None
    get.assert_called_once()
    execute.assert_awaited_once()
    kwargs = execute.await_args.kwargs
    (from_type, to_type), type_id = VOCABULARIES[tenant][4], VOCABULARIES[tenant][5]
    assert kwargs["action"] == "sync"
    assert kwargs["tenant"] == tenant
    assert kwargs["payload"] == {
        "entity_type": VOCABULARIES[tenant][3],
        "batch": [
            {
                "from_id": FROM_ID,
                "to_id": TO_ID,
                "evidence_ref": result.evidence_ref,
                "source_ref": (
                    f"hubspot:crm/v4/associations/{from_type}/{FROM_ID}->{to_type}/{TO_ID}"
                    f"#HUBSPOT_DEFINED:{type_id}"
                ),
                "confidence": 1.0,
                "assertion_status": "observed",
                "polarity": "positive",
            }
        ],
    }  # no source_revision: HubSpot reports none for an association, so none is invented


async def test_replay_reproduces_the_evidence_ref_and_gate_key(
    router: PacketRouter, hubspot: HubSpotClient
) -> None:
    runs = [
        await _produce(
            router, hubspot, "orbit-yard", http=_http(body=_associations_body("orbit-yard"))
        )
        for _ in range(2)
    ]
    (first, _g1, e1), (second, _g2, e2) = runs
    assert first.evidence_ref == second.evidence_ref
    assert first.evidence_ref.startswith("evidence:hubspot:association:")
    assert e1.await_args.kwargs["idempotency_key"] == e2.await_args.kwargs["idempotency_key"]


def test_evidence_ref_separates_tenant_direction_and_type() -> None:
    association = hubspot_module.HubSpotAssociation(
        "companies", FROM_ID, "deals", TO_ID, "HUBSPOT_DEFINED", 342, None
    )
    reversed_association = hubspot_module.HubSpotAssociation(
        "deals", TO_ID, "companies", FROM_ID, "HUBSPOT_DEFINED", 342, None
    )
    base = producer_module.observed_evidence_ref("orbit-yard", _binding("orbit-yard"), association)
    variants = {
        producer_module.observed_evidence_ref("other-yard", _binding("orbit-yard"), association),
        producer_module.observed_evidence_ref(
            "orbit-yard", _binding("orbit-yard"), reversed_association
        ),
        producer_module.observed_evidence_ref(
            "orbit-yard", _binding("orbit-yard", association_type_id=341), association
        ),
        producer_module.observed_evidence_ref(
            "orbit-yard", _binding("orbit-yard", association_category="USER_DEFINED"), association
        ),
    }
    assert base not in variants
    assert len(variants) == 4


# ── Refusals: never a success, and Gate is never called ───────────────────────


@pytest.mark.parametrize(
    ("http", "status"),
    [
        (_http(403, {}), ProducerStatus.PROVIDER_UNAVAILABLE),
        (_http(429, {}), ProducerStatus.PROVIDER_UNAVAILABLE),
        (_http(503, {}), ProducerStatus.PROVIDER_UNAVAILABLE),
        (httpx.ConnectTimeout("timeout"), ProducerStatus.PROVIDER_UNAVAILABLE),
        (_http(body={"results": [{"toObjectId": 1}]}), ProducerStatus.PROVIDER_UNAVAILABLE),
        (_http(body={"results": []}), ProducerStatus.SOURCE_NOT_AVAILABLE),
        (
            _http(body=_associations_body("orbit-yard", to_id="9999")),
            ProducerStatus.SOURCE_NOT_AVAILABLE,
        ),
    ],
    ids=["403", "429", "outage", "timeout", "corrupt", "no-association", "other-record"],
)
async def test_provider_failures_and_absent_associations_send_nothing(
    router: PacketRouter, hubspot: HubSpotClient, http: Any, status: ProducerStatus
) -> None:
    result, _get, execute = await _produce(router, hubspot, "orbit-yard", http=http)
    assert result.status is status
    assert result.ceg_ack is None
    execute.assert_not_awaited()


@pytest.mark.parametrize(
    "binding_overrides",
    [{"association_type_id": 341}, {"association_category": "USER_DEFINED"}],
    ids=["wrong-type-id", "wrong-category"],
)
async def test_an_unbound_association_type_is_not_observed(
    router: PacketRouter, hubspot: HubSpotClient, binding_overrides: dict[str, Any]
) -> None:
    result, _get, execute = await _produce(
        router,
        hubspot,
        "orbit-yard",
        http=_http(body=_associations_body("orbit-yard")),
        binding=_binding("orbit-yard", **binding_overrides),
    )
    assert result.status is ProducerStatus.SOURCE_NOT_AVAILABLE
    execute.assert_not_awaited()


@pytest.mark.parametrize(
    ("tenant", "binding_overrides"),
    [
        ("orbit-yard", {"relation_type": "SUPPLIES"}),
        ("orbit-yard", {"subject_label": "PropellantGrade", "object_label": "OrbitalDepot"}),
        (
            "orbit-yard",
            {
                "relation_type": "TENDED_BY",
                "subject_label": "AgingCave",
                "object_label": "Affineur",
            },
        ),
        ("unknown-tenant", {}),
    ],
    ids=["undeclared-type", "reversed-labels", "foreign-tenant-edge", "no-tenant-spec"],
)
async def test_an_undeclared_tenant_edge_sends_nothing(
    router: PacketRouter, hubspot: HubSpotClient, tenant: str, binding_overrides: dict[str, Any]
) -> None:
    vocabulary = "orbit-yard" if tenant == "unknown-tenant" else tenant
    get = MagicMock(return_value=_http(body=_associations_body(vocabulary)))
    execute = AsyncMock()
    with (
        patch.object(hubspot_module.httpx, "get", get),
        patch.object(router._client, "execute", execute),
    ):
        result = await produce_observed_relationship(
            tenant_id=tenant,
            binding=_binding(vocabulary, **binding_overrides),
            from_record_id=FROM_ID,
            to_record_id=TO_ID,
            hubspot=hubspot,
            router=router,
        )
    assert result.status is ProducerStatus.CONTRACT_NOT_DECLARED
    execute.assert_not_awaited()


async def test_reversed_record_ids_are_not_the_observed_association(
    router: PacketRouter, hubspot: HubSpotClient
) -> None:
    # Swapped ids read the other record's associations of the bound target type,
    # which do not hold the original subject: nothing is observed or sent.
    result, get, execute = await _produce(
        router,
        hubspot,
        "orbit-yard",
        http=_http(body=_associations_body("orbit-yard", to_id="3003")),
        from_id=TO_ID,
        to_id=FROM_ID,
    )
    assert f"/companies/{TO_ID}/associations/deals" in get.call_args.args[0]
    assert result.status is ProducerStatus.SOURCE_NOT_AVAILABLE
    execute.assert_not_awaited()


@pytest.mark.parametrize(
    ("from_id", "to_id"),
    [("", TO_ID), ("abc", TO_ID), (FROM_ID, "2002/../1")],
)
async def test_invalid_record_ids_never_reach_the_provider(
    router: PacketRouter, hubspot: HubSpotClient, from_id: str, to_id: str
) -> None:
    result, get, execute = await _produce(
        router, hubspot, "orbit-yard", http=_http(body={}), from_id=from_id, to_id=to_id
    )
    assert result.status is ProducerStatus.INVALID_INPUT
    get.assert_not_called()
    execute.assert_not_awaited()


@pytest.mark.parametrize(
    "gate",
    [
        GateConnectionError("gate down"),
        create_transport_packet(
            action="sync",
            payload={"status": "success", "entity_type": "stocks", "synced_count": 1},
            tenant="orbit-yard",
            source_node="gate",
            destination_node="enrichment-engine",
            reply_to="gate",
        ),
    ],
    ids=["gate-outage", "node-shaped-ack"],
)
async def test_an_unacknowledged_send_is_not_synced(
    router: PacketRouter, hubspot: HubSpotClient, gate: Any
) -> None:
    result, _get, _execute = await _produce(
        router, hubspot, "orbit-yard", http=_http(body=_associations_body("orbit-yard")), gate=gate
    )
    assert result.status is ProducerStatus.NOT_ACKNOWLEDGED
    assert result.ceg_ack is None


# ── Domain neutrality ─────────────────────────────────────────────────────────


def test_the_producer_holds_no_domain_vocabulary_or_vertical_dependency() -> None:
    source = Path(producer_module.__file__).read_text(encoding="utf-8")
    imported = {
        node.module or ""
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any("feature_evidence" in name or "plasticos" in name.lower() for name in imported)
    for word in (
        "plasticos",
        "recycl",
        "polymer",
        "resin",
        "STOCKS",
        "TENDED_BY",
        "companies",
        "deals",
    ):
        assert word.lower() not in source.lower()
