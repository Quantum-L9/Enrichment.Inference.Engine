"""
tests/unit/test_eie_native_e6_observed_association.py

Campaign EIE-NATIVE-CLOSURE-001, lane L4, slice E6, finding F13.
Acceptance cases O01..O08: the observed CRM association producer
(``app/services/crm/observed_relationship_producer.py``) is operator-invoked
only, refuses an undeclared mapping or a missing endpoint, never fabricates a
node, is idempotent on replay and never crosses tenants.

Fixtures are domain-neutral and SYNTHETIC: two unrelated invented tenants, a
patched ``httpx.get`` shaped as the documented HubSpot v4 associations answer
and a patched ``execute`` on the router's one Gate client. No live HubSpot, Gate
or CEG is reached. The one shipped tenant spec (``domains/plasticos``) is read
only as a regression witness for O01; it is never written.

Live proof of a CEG edge (O03) is the peer campaign's A8; these tests prove the
EIE side of the contract only.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml
from constellation_node_sdk.transport import create_transport_packet

from app.engines.domain_yaml_reader import DomainYamlReader
from app.engines.observed_association_contract import (
    MAPPING_UNVERIFIED_MIRROR,
    mirror_drift,
)
from app.engines.packet_router import PacketRouter
from app.services.crm import hubspot_client as hubspot_module
from app.services.crm.base import CRMCredentials, CRMType
from app.services.crm.hubspot_client import HubSpotClient
from app.services.crm.observed_relationship_producer import (
    ObservedAssociationKey,
    ProducerStatus,
    produce_observed_relationship,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCER_MODULE = "app.services.crm.observed_relationship_producer"

# tenant -> (subject label, relation, object label, edge entity type, provider pair, typeId)
TENANTS: dict[str, tuple[str, str, str, str, tuple[str, str], int]] = {
    "tenant-alpha": ("Holder", "HOLDS", "Item", "holds", ("companies", "deals"), 341),
    "tenant-beta": ("Keeper", "KEEPS", "Thing", "keeps", ("deals", "contacts"), 5),
}
FROM_ID = "7001"
TO_ID = "8002"


def _spec(tenant: str, *, type_id: int | None = None) -> dict[str, Any]:
    subject, relation, obj, entity, (from_type, to_type), declared = TENANTS[tenant]
    return {
        "domain": {"id": tenant, "name": "synthetic", "version": "0.1.0"},
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
                    "refnamespace": f"{tenant}.subject",
                },
                {
                    "path": "/v1/sync/objects",
                    "targetnode": obj,
                    "idproperty": "object_id",
                    "batchstrategy": "UNWINDMERGE",
                    "refnamespace": f"{tenant}.object",
                },
                {
                    "path": f"/v1/sync/{entity}",
                    "targetedge": relation,
                    "fromnode": subject,
                    "tonode": obj,
                    "batchstrategy": "UNWINDMERGE",
                    "observedfrom": [
                        {
                            "provider": "hubspot",
                            "fromobjecttype": from_type,
                            "toobjecttype": to_type,
                            "associationcategory": "HUBSPOT_DEFINED",
                            "associationtypeid": type_id if type_id is not None else declared,
                        }
                    ],
                },
            ]
        },
    }


def _spec_without_mapping(tenant: str) -> dict[str, Any]:
    """The same tenant graph with no ``observedfrom`` anywhere: no approved mapping."""
    spec = _spec(tenant)
    for endpoint in spec["sync"]["endpoints"]:
        endpoint.pop("observedfrom", None)
    return spec


def _key(tenant: str, **overrides: Any) -> ObservedAssociationKey:
    *_labels, (from_type, to_type), type_id = TENANTS[tenant]
    fields: dict[str, Any] = {
        "from_object_type": from_type,
        "to_object_type": to_type,
        "association_category": "HUBSPOT_DEFINED",
        "association_type_id": type_id,
    }
    fields.update(overrides)
    return ObservedAssociationKey(**fields)


def _router(root: Path, specs: dict[str, dict[str, Any]]) -> PacketRouter:
    for tenant, spec in specs.items():
        (root / tenant).mkdir(parents=True)
        (root / tenant / "spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    return PacketRouter(gate_url="https://gate-node:8080", domain_reader=DomainYamlReader(root))


def _http(tenant: str, *, to_id: str = TO_ID) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.headers = {}
    response.json.return_value = {
        "results": [
            {
                "toObjectId": int(to_id),
                "associationTypes": [
                    {"category": "HUBSPOT_DEFINED", "typeId": TENANTS[tenant][5], "label": None},
                    {"category": "USER_DEFINED", "typeId": 99, "label": "Synthetic label"},
                ],
            }
        ]
    }
    return response


def _ack(tenant: str, *, created: int = 1, replayed: int = 0) -> Any:
    return create_transport_packet(
        action="sync",
        payload={
            "status": "success",
            "entity_type": TENANTS[tenant][3],
            "synced_count": 1,
            "created_count": created,
            "replayed_count": replayed,
        },
        tenant=tenant,
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )


def _endpoint_missing_refusal(tenant: str) -> Any:
    """CEG's failure answer when an edge endpoint node does not exist (class name only)."""
    return _ack(tenant).derive(
        packet_type="failure",
        source_node="graph-engine",
        destination_node="enrichment-engine",
        reply_to="graph-engine",
        payload={
            "status": "failed",
            "error": "RelationshipEndpointNotFoundError",
            "message": "RelationshipEndpointNotFoundError",
        },
    )


@pytest.fixture
def hubspot() -> HubSpotClient:
    return HubSpotClient(
        CRMCredentials(crm_type=CRMType.HUBSPOT, credentials={"access_token": "t"})
    )


@pytest.fixture
def router(tmp_path: Path) -> PacketRouter:
    return _router(tmp_path / "domains", {t: _spec(t) for t in TENANTS})


async def _produce(
    router: PacketRouter,
    hubspot: HubSpotClient,
    tenant: str,
    *,
    http: Any,
    key: ObservedAssociationKey | None = None,
    gate: Any = None,
    from_id: str = FROM_ID,
    to_id: str = TO_ID,
) -> tuple[Any, MagicMock, AsyncMock]:
    get = MagicMock(return_value=http)
    execute = AsyncMock(return_value=gate if gate is not None else _ack(tenant))
    with (
        patch.object(hubspot_module.httpx, "get", get),
        patch.object(router._client, "execute", execute),
        patch("app.engines.packet_router.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await produce_observed_relationship(
            tenant_id=tenant,
            key=key or _key(tenant),
            from_record_id=from_id,
            to_record_id=to_id,
            hubspot=hubspot,
            router=router,
            send_observed_association=False,
        )
    return result, get, execute


# ── O01: no approved mapping => no relationship obligation ────────────────────


def test_o01_shipped_tenant_spec_declares_no_observed_association() -> None:
    """Regression witness: the one shipped tenant (PlasticOS) has no approved mapping.

    So no tenant can have a relationship produced today, and live E6 is blocked on
    an owner declaring ``observedfrom`` in CEG's tenant spec (and its EIE mirror).
    """
    domains = REPO_ROOT / "domains"
    specs = sorted(domains.glob("*/spec.yaml")) + sorted(domains.glob("*/revisions/**/*.yaml"))
    assert specs, "expected at least one shipped tenant spec"
    for path in specs:
        assert "observedfrom" not in path.read_text(encoding="utf-8"), path

    shipped = PacketRouter(
        gate_url="https://gate-node:8080", domain_reader=DomainYamlReader(domains)
    )
    for tenant in sorted(p.name for p in domains.iterdir() if p.is_dir()):
        for from_type, to_type in (("companies", "deals"), ("companies", "contacts")):
            assert (
                shipped.observed_association_contract(
                    tenant,
                    provider="hubspot",
                    from_object_type=from_type,
                    to_object_type=to_type,
                    association_category="HUBSPOT_DEFINED",
                    association_type_id=1,
                )
                is None
            )


async def test_o01_an_edge_without_observedfrom_creates_no_obligation(
    tmp_path: Path, hubspot: HubSpotClient
) -> None:
    router = _router(tmp_path / "domains", {"tenant-alpha": _spec_without_mapping("tenant-alpha")})
    result, get, execute = await _produce(
        router, hubspot, "tenant-alpha", http=_http("tenant-alpha")
    )
    assert result.status is ProducerStatus.CONTRACT_NOT_DECLARED
    get.assert_not_called()
    execute.assert_not_awaited()


# ── O02 / O03: approved association => one declared relationship, one evidence_ref


@pytest.mark.parametrize("tenant", sorted(TENANTS))
async def test_o02_o03_approved_association_yields_one_relationship_with_one_evidence_ref(
    router: PacketRouter, hubspot: HubSpotClient, tenant: str
) -> None:
    subject, relation, obj, entity, (from_type, to_type), type_id = TENANTS[tenant]
    result, get, execute = await _produce(router, hubspot, tenant, http=_http(tenant))

    assert result.status is ProducerStatus.SYNCED
    assert result.mapping_verification == MAPPING_UNVERIFIED_MIRROR
    assert result.evidence_ref is not None
    assert result.evidence_ref.startswith("evidence:hubspot:association:")
    get.assert_called_once()
    execute.assert_awaited_once()
    kwargs = execute.await_args.kwargs
    assert kwargs["action"] == "sync"
    assert kwargs["tenant"] == tenant
    payload = kwargs["payload"]
    assert payload["entity_type"] == entity  # the declared edge endpoint, never a node
    (row,) = payload["batch"]
    assert row["from_id"] == f"{tenant}.subject:{FROM_ID}"
    assert row["to_id"] == f"{tenant}.object:{TO_ID}"
    assert row["evidence_ref"] == result.evidence_ref
    assert row["source_ref"] == (
        f"hubspot:crm/associations/{from_type}/{FROM_ID}->{to_type}/{TO_ID}"
        f"#HUBSPOT_DEFINED:{type_id}"
    )
    assert row["assertion_status"] == "observed"


# ── O04: replay => stable evidence identity, same Gate key ────────────────────


async def test_o04_replay_keeps_evidence_identity_and_gate_key(
    router: PacketRouter, hubspot: HubSpotClient
) -> None:
    first, _g1, e1 = await _produce(router, hubspot, "tenant-alpha", http=_http("tenant-alpha"))
    second, _g2, e2 = await _produce(
        router,
        hubspot,
        "tenant-alpha",
        http=_http("tenant-alpha"),
        gate=_ack("tenant-alpha", created=0, replayed=1),
    )
    assert first.status is second.status is ProducerStatus.SYNCED
    assert first.evidence_ref == second.evidence_ref
    assert first.source_ref == second.source_ref
    k1, k2 = e1.await_args.kwargs, e2.await_args.kwargs
    assert k1["idempotency_key"] == k2["idempotency_key"]
    assert k1["payload"] == k2["payload"]  # byte-identical row: nothing new to create


# ── O05: unknown category / wrong type / wrong provider pair => refused early ─


@pytest.mark.parametrize(
    "overrides",
    [
        {"association_category": "USER_DEFINED"},
        {"association_category": "UNKNOWN_CATEGORY"},
        {"association_type_id": 99},
        {"from_object_type": "contacts"},
        {"to_object_type": "tickets"},
        {"from_object_type": "deals", "to_object_type": "companies"},  # reversed pair
    ],
    ids=["user-defined", "unknown-category", "wrong-type-id", "wrong-from", "wrong-to", "reversed"],
)
async def test_o05_undeclared_association_is_refused_before_mapping_or_graph(
    router: PacketRouter, hubspot: HubSpotClient, overrides: dict[str, Any]
) -> None:
    result, get, execute = await _produce(
        router,
        hubspot,
        "tenant-alpha",
        http=_http("tenant-alpha"),
        key=_key("tenant-alpha", **overrides),
    )
    assert result.status is ProducerStatus.CONTRACT_NOT_DECLARED
    assert result.evidence_ref is None
    get.assert_not_called()  # refused before provider I/O
    execute.assert_not_awaited()  # nothing reaches Gate/CEG


# ── O06: mapped association, endpoint node missing => refused, no node made ──


async def test_o06_missing_endpoint_is_refused_and_no_node_is_fabricated(
    router: PacketRouter, hubspot: HubSpotClient
) -> None:
    result, _get, execute = await _produce(
        router,
        hubspot,
        "tenant-alpha",
        http=_http("tenant-alpha"),
        gate=_endpoint_missing_refusal("tenant-alpha"),
    )
    assert result.status is ProducerStatus.NOT_ACKNOWLEDGED
    assert result.ceg_ack is None
    # Exactly one edge sync, never retried, and no node-endpoint sync was attempted.
    assert execute.await_count == 1
    node_entities = {"subjects", "objects"}
    for call in execute.await_args_list:
        assert call.kwargs["payload"]["entity_type"] not in node_entities
        for row in call.kwargs["payload"]["batch"]:
            assert set(row) >= {"from_id", "to_id", "evidence_ref"}


async def test_o06_endpoint_without_refnamespace_is_not_declared(
    tmp_path: Path, hubspot: HubSpotClient
) -> None:
    spec = _spec("tenant-alpha")
    spec["sync"]["endpoints"][1].pop("refnamespace")  # object node has no graph-ref identity
    router = _router(tmp_path / "domains", {"tenant-alpha": spec})
    result, get, execute = await _produce(
        router, hubspot, "tenant-alpha", http=_http("tenant-alpha")
    )
    assert result.status is ProducerStatus.CONTRACT_NOT_DECLARED
    get.assert_not_called()
    execute.assert_not_awaited()


# ── O07: foreign tenant / wrong mirror => no cross-tenant edge ────────────────


async def test_o07_another_tenants_association_is_not_declared_here(
    router: PacketRouter, hubspot: HubSpotClient
) -> None:
    # tenant-beta asked for tenant-alpha's declared association.
    result, get, execute = await _produce(
        router, hubspot, "tenant-beta", http=_http("tenant-alpha"), key=_key("tenant-alpha")
    )
    assert result.status is ProducerStatus.CONTRACT_NOT_DECLARED
    get.assert_not_called()
    execute.assert_not_awaited()


@pytest.mark.parametrize("tenant", sorted(TENANTS))
async def test_o07_refs_and_packet_tenant_never_leave_the_invoking_tenant(
    router: PacketRouter, hubspot: HubSpotClient, tenant: str
) -> None:
    other = next(t for t in TENANTS if t != tenant)
    _result, _get, execute = await _produce(router, hubspot, tenant, http=_http(tenant))
    kwargs = execute.await_args.kwargs
    assert kwargs["tenant"] == tenant
    (row,) = kwargs["payload"]["batch"]
    for ref in (row["from_id"], row["to_id"]):
        assert ref.startswith(f"{tenant}.")
        assert other not in ref


async def test_o07_unknown_tenant_has_no_mapping(
    router: PacketRouter, hubspot: HubSpotClient
) -> None:
    result, get, execute = await _produce(
        router,
        hubspot,
        "tenant-absent",
        http=_http("tenant-alpha"),
        key=_key("tenant-alpha"),
        gate=_ack("tenant-alpha"),
    )
    assert result.status is ProducerStatus.CONTRACT_NOT_DECLARED
    get.assert_not_called()
    execute.assert_not_awaited()


def test_o07_a_mirror_mapping_a_different_type_is_drift_against_ceg() -> None:
    authoritative = _spec("tenant-alpha")
    wrong_mirror = _spec("tenant-alpha", type_id=999)
    assert mirror_drift(wrong_mirror, authoritative)
    assert mirror_drift(authoritative, authoritative) == []


# ── O08: ordinary enrich/converge never runs the producer implicitly ─────────


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_o08_no_application_module_imports_or_calls_the_producer() -> None:
    producer = REPO_ROOT / "app/services/crm/observed_relationship_producer.py"
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        if path == producer:
            continue
        source = path.read_text(encoding="utf-8")
        imported = _imports(path)
        if any("observed_relationship_producer" in name for name in imported):
            offenders.append(f"{path.relative_to(REPO_ROOT)}: imports producer")
        if "produce_observed_relationship" in source:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: names produce_observed_relationship")
        if ".read_associations(" in source or "read_associations," in source:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: reads provider associations")
    assert offenders == []


def test_o08_loading_the_node_and_its_enrich_converge_handlers_never_loads_the_producer() -> None:
    probe = (
        "import sys\n"
        "import app.main, app.engines.handlers, app.engines.orchestration_layer\n"
        "import app.engines.sweep_handlers, app.services.chassis_handlers\n"
        f"print({PRODUCER_MODULE!r} in sys.modules)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip().splitlines()[-1] == "False"
