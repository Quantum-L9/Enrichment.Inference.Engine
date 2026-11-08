"""
tests/unit/test_observed_relationship_producer.py

QM.POC0.EIE.OBSERVED_RELATIONSHIP_PRODUCER.003 — one observed HubSpot
association becomes one RelationshipAssertion over the existing
``PacketRouter.notify_relationship_sync``. Since
QM.POC0.TENANT_ASSOCIATION_MAPPING.004 the operator names only the provider
association (``ObservedAssociationKey``); the tenant spec's ``observedfrom`` and
``refnamespace`` decide the relationship and the node refs.

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
from constellation_node_sdk.gate import (
    GateConnectionError,
    GateHTTPError,
    GateTimeoutError,
)
from constellation_node_sdk.transport import create_transport_packet

from app.engines.domain_yaml_reader import DomainYamlReader
from app.engines.observed_association_contract import (
    MAPPING_UNVERIFIED_MIRROR,
    ObservedRelationshipContract,
    mapping_fingerprint,
)
from app.engines.packet_router import PacketRouter, RelationshipSyncContract
from app.services.crm import hubspot_client as hubspot_module
from app.services.crm import observed_relationship_producer as producer_module
from app.services.crm.base import CRMCredentials, CRMType
from app.services.crm.hubspot_client import HubSpotAssociationReadError, HubSpotClient
from app.services.crm.observed_relationship_producer import (
    ObservedAssociationKey,
    ProducerStatus,
    produce_observed_relationship,
)

pytestmark = pytest.mark.unit


def _spec(
    domain_id: str,
    subject: str,
    relation: str,
    obj: str,
    entity_type: str,
    provider_pair: tuple[str, str],
    type_id: int,
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
                    "path": "/v1/sync/subjects",
                    "targetnode": subject,
                    "idproperty": "subject_id",
                    "batchstrategy": "UNWINDMERGE",
                    "refnamespace": f"{domain_id}.subject",
                },
                {
                    "path": "/v1/sync/objects",
                    "targetnode": obj,
                    "idproperty": "object_id",
                    "batchstrategy": "UNWINDMERGE",
                    "refnamespace": f"{domain_id}.object",
                },
                {
                    "path": f"/v1/sync/{entity_type}",
                    "targetedge": relation,
                    "fromnode": subject,
                    "tonode": obj,
                    "batchstrategy": "UNWINDMERGE",
                    "observedfrom": [
                        {
                            "provider": "hubspot",
                            "fromobjecttype": provider_pair[0],
                            "toobjecttype": provider_pair[1],
                            "associationcategory": "HUBSPOT_DEFINED",
                            "associationtypeid": type_id,
                        }
                    ],
                },
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


def _tenant_spec(tenant: str) -> dict[str, Any]:
    subject, relation, obj, entity_type, pair, type_id = VOCABULARIES[tenant]
    return _spec(tenant, subject, relation, obj, entity_type, pair, type_id)


def _key(tenant: str, **overrides: Any) -> ObservedAssociationKey:
    _subject, _relation, _obj, _entity, (from_type, to_type), type_id = VOCABULARIES[tenant]
    fields: dict[str, Any] = {
        "from_object_type": from_type,
        "to_object_type": to_type,
        "association_category": "HUBSPOT_DEFINED",
        "association_type_id": type_id,
    }
    fields.update(overrides)
    return ObservedAssociationKey(**fields)


def _router_over(root: Path, specs: dict[str, dict[str, Any]]) -> PacketRouter:
    for tenant, spec in specs.items():
        (root / tenant).mkdir(parents=True)
        (root / tenant / "spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    return PacketRouter(gate_url="https://gate-node:8080", domain_reader=DomainYamlReader(root))


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
    return _router_over(tmp_path / "domains", {t: _tenant_spec(t) for t in VOCABULARIES})


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
    key: ObservedAssociationKey | None = None,
    from_id: str = FROM_ID,
    to_id: str = TO_ID,
    send_observed_association: bool | None = None,
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
            key=key or _key(tenant),
            from_record_id=from_id,
            to_record_id=to_id,
            hubspot=hubspot,
            router=router,
            send_observed_association=send_observed_association,
        )
    return result, get, execute


# ── HubSpot association read (read-only, fail closed) ─────────────────────────


def test_read_associations_returns_every_reported_type(hubspot: HubSpotClient) -> None:
    get = MagicMock(return_value=_http(body=_associations_body("orbit-yard")))
    with patch.object(hubspot_module.httpx, "get", get):
        associations = hubspot.read_associations("company", FROM_ID, "deal")
    url = get.call_args.args[0]
    assert (
        url == f"https://api.hubapi.com/crm/objects/2026-09/companies/{FROM_ID}/associations/deals"
    )
    assert "params" not in get.call_args.kwargs  # no undocumented query parameter is sent
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
        {"results": [], "paging": {"next": {}}},
        {"results": [], "paging": "corrupt"},
    ],
    ids=[
        "no-body",
        "results-not-list",
        "no-to-id",
        "no-types",
        "no-type-id",
        "type-id-text",
        "truncated",
        "empty-next",
        "paging-not-object",
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


def test_malformed_rate_limit_headers_raise_the_read_error(hubspot: HubSpotClient) -> None:
    response = _http(body=_associations_body("orbit-yard"))
    response.headers = {"X-HubSpot-RateLimit-Remaining": "n/a"}
    with (
        patch.object(hubspot_module.httpx, "get", MagicMock(return_value=response)),
        pytest.raises(HubSpotAssociationReadError, match="rate-limit"),
    ):
        hubspot.read_associations("companies", FROM_ID, "deals")


@pytest.mark.parametrize(
    ("from_type", "record_id"),
    [
        ("companies", "../deals"),
        ("companies", "12a"),
        ("companies", "0123"),
        ("companies", "０１"),
        ("companies", "٣"),
        ("companies/../x", FROM_ID),
        ("Deals", FROM_ID),
    ],
    ids=[
        "traversal",
        "alnum",
        "leading-zero",
        "fullwidth-digits",
        "arabic-digit",
        "type-path",
        "type-case",
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
                "from_id": f"{tenant}.subject:{FROM_ID}",
                "to_id": f"{tenant}.object:{TO_ID}",
                "evidence_ref": result.evidence_ref,
                "source_ref": (
                    f"hubspot:crm/associations/{from_type}/{FROM_ID}->{to_type}/{TO_ID}"
                    f"#HUBSPOT_DEFINED:{type_id}"
                ),
                "confidence": 1.0,
                "assertion_status": "observed",
                "polarity": "positive",
            }
        ],
    }  # no source_revision: HubSpot reports none for an association, so none is invented


# Golden: the values the predecessor (EIE #256, operator-supplied binding) produced
# for the same association. Moving the mapping into the spec changes none of them.
GOLDEN = {
    "orbit-yard": (
        "evidence:hubspot:association:a7dfc7ac47114097f8a05629",
        "eie:relsync:orbit-yard:evidence:hubspot:association:a7dfc7ac47114097f8a05629:7d2d2d9d18bba05b",
    ),
    "cave-works": (
        "evidence:hubspot:association:2c08674ed058d7985e2aa34e",
        "eie:relsync:cave-works:evidence:hubspot:association:2c08674ed058d7985e2aa34e:3836cf8a57ef9f5b",
    ),
}


@pytest.mark.parametrize("tenant", sorted(VOCABULARIES))
async def test_replay_reproduces_the_evidence_ref_and_gate_key(
    router: PacketRouter, hubspot: HubSpotClient, tenant: str
) -> None:
    runs = [
        await _produce(router, hubspot, tenant, http=_http(body=_associations_body(tenant)))
        for _ in range(2)
    ]
    (first, _g1, e1), (second, _g2, e2) = runs
    assert first.evidence_ref == second.evidence_ref
    assert e1.await_args.kwargs["idempotency_key"] == e2.await_args.kwargs["idempotency_key"]
    assert (first.evidence_ref, e1.await_args.kwargs["idempotency_key"]) == GOLDEN[tenant]


def _contract(**overrides: Any) -> ObservedRelationshipContract:
    fields: dict[str, Any] = {
        "sync": RelationshipSyncContract("stocks", "STOCKS", "OrbitalDepot", "PropellantGrade"),
        "provider": "hubspot",
        "from_object_type": "companies",
        "to_object_type": "deals",
        "association_category": "HUBSPOT_DEFINED",
        "association_type_id": 342,
        "subject_ref_namespace": "orbit-yard.subject",
        "object_ref_namespace": "orbit-yard.object",
        "mapping_fingerprint": "sha256:synthetic",
    }
    fields.update(overrides)
    return ObservedRelationshipContract(**fields)


def test_evidence_ref_separates_tenant_direction_type_and_identity_but_not_labels() -> None:
    association = hubspot_module.HubSpotAssociation(
        "companies", FROM_ID, "deals", TO_ID, "HUBSPOT_DEFINED", 342, None
    )
    reversed_association = hubspot_module.HubSpotAssociation(
        "deals", TO_ID, "companies", FROM_ID, "HUBSPOT_DEFINED", 342, None
    )
    ref = producer_module.observed_evidence_ref
    base = ref("orbit-yard", _contract(), association)
    variants = {
        ref("other-yard", _contract(), association),
        ref("orbit-yard", _contract(), reversed_association),
        ref("orbit-yard", _contract(association_type_id=341), association),
        ref("orbit-yard", _contract(association_category="USER_DEFINED"), association),
        ref("orbit-yard", _contract(object_ref_namespace="other.object"), association),
    }
    assert base not in variants
    assert len(variants) == 5
    # Labels, relation type and the mapping digest never enter the identity.
    relabelled = _contract(
        sync=RelationshipSyncContract("other", "OTHER", "A", "B"), mapping_fingerprint="sha256:x"
    )
    assert ref("orbit-yard", relabelled, association) == base


def test_the_key_cannot_carry_tenant_edge_semantics() -> None:
    for field in ("subject_label", "relation_type", "object_label", "subject_ref_namespace"):
        with pytest.raises(TypeError):
            ObservedAssociationKey(  # type: ignore[call-arg]
                "companies", "deals", "HUBSPOT_DEFINED", 342, **{field: "X"}
            )


@pytest.mark.parametrize(
    "overrides",
    [
        {"association_type_id": 0},
        {"association_type_id": True},
        {"association_category": " "},
        {"from_object_type": ""},
    ],
    ids=["zero-id", "bool-id", "blank-category", "blank-type"],
)
async def test_an_invalid_key_never_reaches_the_provider(
    router: PacketRouter, hubspot: HubSpotClient, overrides: dict[str, Any]
) -> None:
    result, get, execute = await _produce(
        router,
        hubspot,
        "orbit-yard",
        http=_http(body=_associations_body("orbit-yard")),
        key=_key("orbit-yard", **overrides),
    )
    assert result.status is ProducerStatus.INVALID_INPUT
    assert result.mapping_verification is None
    get.assert_not_called()
    execute.assert_not_awaited()


async def test_an_eie_alias_resolves_only_through_the_provider_native_name(
    router: PacketRouter, hubspot: HubSpotClient
) -> None:
    aliased, get, _execute = await _produce(
        router,
        hubspot,
        "orbit-yard",
        http=_http(body=_associations_body("orbit-yard")),
        key=_key("orbit-yard", from_object_type="company", to_object_type="deal"),
    )
    assert aliased.status is ProducerStatus.SYNCED
    assert "/companies/" in get.call_args.args[0]
    unknown, get, _execute = await _produce(
        router,
        hubspot,
        "orbit-yard",
        http=_http(body=_associations_body("orbit-yard")),
        key=_key("orbit-yard", from_object_type="firm"),
    )
    assert unknown.status is ProducerStatus.CONTRACT_NOT_DECLARED
    get.assert_not_called()


async def test_a_resolved_result_names_the_unverified_mirror_it_came_from(
    router: PacketRouter, hubspot: HubSpotClient
) -> None:
    result, _get, _execute = await _produce(
        router, hubspot, "orbit-yard", http=_http(body=_associations_body("orbit-yard"))
    )
    assert result.status is ProducerStatus.SYNCED
    assert result.mapping_verification == MAPPING_UNVERIFIED_MIRROR
    assert result.mapping_fingerprint == mapping_fingerprint(_tenant_spec("orbit-yard"))


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
    assert result.mapping_verification == MAPPING_UNVERIFIED_MIRROR
    execute.assert_not_awaited()


async def test_a_declared_type_the_provider_does_not_report_is_not_observed(
    router: PacketRouter, hubspot: HubSpotClient
) -> None:
    body = _associations_body("orbit-yard")
    body["results"][0]["associationTypes"] = [
        {"category": "HUBSPOT_DEFINED", "typeId": 341, "label": None}
    ]
    result, get, execute = await _produce(router, hubspot, "orbit-yard", http=_http(body=body))
    assert result.status is ProducerStatus.SOURCE_NOT_AVAILABLE
    get.assert_called_once()
    execute.assert_not_awaited()


def _mutated(mutate: Any) -> dict[str, Any]:
    spec = _tenant_spec("orbit-yard")
    mutate(spec)
    return spec


def _relationship_endpoint(spec: dict[str, Any]) -> dict[str, Any]:
    return next(e for e in spec["sync"]["endpoints"] if e.get("targetedge"))


@pytest.mark.parametrize(
    ("spec", "key_overrides"),
    [
        (None, {"association_type_id": 341}),
        (None, {"association_category": "USER_DEFINED"}),
        (None, {"from_object_type": "deals", "to_object_type": "companies"}),
        (
            None,
            {"from_object_type": "deals", "to_object_type": "contacts", "association_type_id": 3},
        ),
        (
            _mutated(
                lambda s: _relationship_endpoint(s)["observedfrom"].append(
                    dict(_relationship_endpoint(s)["observedfrom"][0])
                )
            ),
            {},
        ),
        (
            _mutated(
                lambda s: _relationship_endpoint(s)["observedfrom"][0].update(
                    associationtypeid="342"
                )
            ),
            {},
        ),
        (_mutated(lambda s: s["sync"]["endpoints"][1].pop("refnamespace")), {}),
        (_mutated(lambda s: s["sync"]["endpoints"][1].update(refnamespace="Has Space")), {}),
        (_mutated(lambda s: s["ontology"]["edges"][0].update(direction="UNDIRECTED")), {}),
        (
            _mutated(
                lambda s: s["ontology"]["edges"][0].update(
                    {"from": "PropellantGrade", "to": "OrbitalDepot"}
                )
            ),
            {},
        ),
        ("no-spec", {}),
    ],
    ids=[
        "undeclared-type-id",
        "undeclared-category",
        "reversed-object-types",
        "foreign-tenant-key",
        "declared-twice",
        "malformed-entry",
        "object-namespace-missing",
        "object-namespace-malformed",
        "undirected-edge",
        "reversed-edge",
        "no-tenant-spec",
    ],
)
async def test_an_unresolvable_mapping_reads_nothing_and_sends_nothing(
    tmp_path: Path, hubspot: HubSpotClient, spec: Any, key_overrides: dict[str, Any]
) -> None:
    specs = {} if spec == "no-spec" else {"orbit-yard": spec or _tenant_spec("orbit-yard")}
    router = _router_over(tmp_path / "domains", specs)
    result, get, execute = await _produce(
        router,
        hubspot,
        "orbit-yard",
        http=_http(body=_associations_body("orbit-yard")),
        key=_key("orbit-yard", **key_overrides),
    )
    assert result.status is ProducerStatus.CONTRACT_NOT_DECLARED
    assert result.mapping_verification is None
    get.assert_not_called()  # resolved before any provider I/O
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
    [("", TO_ID), ("abc", TO_ID), (FROM_ID, "2002/../1"), ("0123", TO_ID), (FROM_ID, "\u0663")],
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
        GateTimeoutError("deadline elapsed", timeout_seconds=10.0),
        GateHTTPError("gate overloaded", status_code=503),
    ],
    ids=["gate-unreachable", "gate-timeout", "gate-5xx"],
)
async def test_a_gate_outage_is_transport_unavailable_not_a_refusal(
    router: PacketRouter, hubspot: HubSpotClient, gate: Exception
) -> None:
    with patch("app.engines.packet_router.asyncio.sleep", new_callable=AsyncMock):
        result, _get, execute = await _produce(
            router,
            hubspot,
            "orbit-yard",
            http=_http(body=_associations_body("orbit-yard")),
            gate=gate,
        )
    assert result.status is ProducerStatus.TRANSPORT_UNAVAILABLE
    assert result.ceg_ack is None
    assert result.evidence_ref is not None and result.source_ref is not None
    assert execute.await_count == 2  # retries under the pinned key were spent first


def _failure_packet() -> Any:
    """Gate relays a worker ``failure`` packet with HTTP 200; its error name is never read."""
    request = _ack("orbit-yard", "stocks")
    return request.derive(
        packet_type="failure",
        source_node="graph-engine",
        destination_node="enrichment-engine",
        reply_to="graph-engine",
        payload={"status": "failed", "error": "AnyWorkerError", "message": "refused"},
    )


@pytest.mark.parametrize(
    "gate",
    [
        GateHTTPError("unprocessable", status_code=422),
        _failure_packet(),
        create_transport_packet(
            action="sync",
            payload={"status": "success", "entity_type": "stocks", "synced_count": 1},
            tenant="orbit-yard",
            source_node="gate",
            destination_node="enrichment-engine",
            reply_to="gate",
        ),
    ],
    ids=["gate-4xx", "worker-failure-packet", "node-shaped-ack"],
)
async def test_a_refused_or_unconfirmed_send_is_not_acknowledged(
    router: PacketRouter, hubspot: HubSpotClient, gate: Any
) -> None:
    with patch("app.engines.packet_router.asyncio.sleep", new_callable=AsyncMock):
        result, _get, execute = await _produce(
            router,
            hubspot,
            "orbit-yard",
            http=_http(body=_associations_body("orbit-yard")),
            gate=gate,
        )
    assert result.status is ProducerStatus.NOT_ACKNOWLEDGED
    assert result.ceg_ack is None
    assert execute.await_count == 1  # a refusal is never retried


# ── D-9 (Cognitive.Engine.Graphs#386): CEG cross-checks the mirror's mapping ──


def test_sending_the_association_ships_off_until_ceg_accepts_it() -> None:
    from app.core.config import Settings

    assert Settings.model_fields["observed_association_send_enabled"].default is False


@pytest.mark.parametrize("tenant", sorted(VOCABULARIES))
async def test_the_row_carries_the_association_the_mirror_mapped(
    router: PacketRouter, hubspot: HubSpotClient, tenant: str
) -> None:
    result, _get, execute = await _produce(
        router,
        hubspot,
        tenant,
        http=_http(body=_associations_body(tenant)),
        send_observed_association=True,
    )
    assert result.status is ProducerStatus.SYNCED
    (from_type, to_type), type_id = VOCABULARIES[tenant][4], VOCABULARIES[tenant][5]
    (row,) = execute.await_args.kwargs["payload"]["batch"]
    assert row["observed_association"] == {
        "provider": "hubspot",
        "fromobjecttype": from_type,
        "toobjecttype": to_type,
        "associationcategory": "HUBSPOT_DEFINED",
        "associationtypeid": type_id,
    }


async def test_the_association_is_not_sent_unless_enabled(
    router: PacketRouter, hubspot: HubSpotClient
) -> None:
    _result, _get, execute = await _produce(
        router,
        hubspot,
        "orbit-yard",
        http=_http(body=_associations_body("orbit-yard")),
        send_observed_association=False,
    )
    (row,) = execute.await_args.kwargs["payload"]["batch"]
    assert "observed_association" not in row


def _ceg_rejected_packet() -> Any:
    """CEG's answer when its own spec does not declare the association (class name only)."""
    request = _ack("orbit-yard", "stocks")
    return request.derive(
        packet_type="failure",
        source_node="graph-engine",
        destination_node="enrichment-engine",
        reply_to="graph-engine",
        payload={
            "status": "failed",
            "error": "ObservedAssociationRejectedError",
            "message": "ObservedAssociationRejectedError",
        },
    )


async def test_a_ceg_refusal_of_the_association_is_reported_as_mirror_drift(
    router: PacketRouter, hubspot: HubSpotClient
) -> None:
    result, _get, execute = await _produce(
        router,
        hubspot,
        "orbit-yard",
        http=_http(body=_associations_body("orbit-yard")),
        gate=_ceg_rejected_packet(),
        send_observed_association=True,
    )
    assert result.status is ProducerStatus.MAPPING_REJECTED_BY_CEG
    assert "drifted" in result.detail
    assert result.mapping_fingerprint is not None
    assert result.ceg_ack is None
    assert execute.await_count == 1  # a refusal is never retried


async def test_drift_is_claimed_only_when_the_association_was_sent(
    router: PacketRouter, hubspot: HubSpotClient
) -> None:
    result, _get, _execute = await _produce(
        router,
        hubspot,
        "orbit-yard",
        http=_http(body=_associations_body("orbit-yard")),
        gate=_ceg_rejected_packet(),
        send_observed_association=False,
    )
    assert result.status is ProducerStatus.NOT_ACKNOWLEDGED


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
