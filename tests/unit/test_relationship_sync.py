"""
tests/unit/test_relationship_sync.py

QM.POC0.EIE.GRAPH_EVIDENCE.002B — EIE proposes one evidenced, typed relationship
over the existing Gate ``sync`` action, at the endpoint the tenant's spec
declares (``targetedge`` / ``fromnode`` / ``tonode``, CEG
QM.POC0.CEG.GRAPH_EVIDENCE.002A).

Gate is replaced by a patched ``execute`` on the router's one Gate client, as in
tests/test_pr21_packet_router.py. The two vocabularies are synthetic and
unrelated; neither names a product domain.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from constellation_node_sdk.gate import (
    GateAuthorizationError,
    GateConnectionError,
    GateHTTPError,
    GateTimeoutError,
)
from constellation_node_sdk.transport import create_transport_packet
from constellation_node_sdk.transport.hop_trace import make_response_hop

from app.engines.domain_yaml_reader import DomainYamlReader
from app.engines.packet_router import (
    PacketRouter,
    RelationshipAssertion,
    RelationshipSyncContract,
    RelationshipSyncOutcome,
    RelationshipSyncResult,
    _relationship_idempotency_key,
    relationship_assertion_defects,
    relationship_sync_acknowledged,
    resolve_graph_sync_contract,
    resolve_relationship_sync_contract,
)

pytestmark = pytest.mark.unit

_ORBITAL_SPEC: dict[str, Any] = {
    "domain": {"id": "orbit-yard", "name": "synthetic", "version": "0.1.0"},
    "ontology": {
        "nodes": [
            {"label": "OrbitalDepot", "properties": [{"name": "depot_id", "required": True}]},
            {"label": "PropellantGrade", "properties": [{"name": "grade_code", "required": True}]},
        ],
        "edges": [
            {
                "type": "STOCKS",
                "from": "OrbitalDepot",
                "to": "PropellantGrade",
                "direction": "DIRECTED",
                "managedby": "sync",
            },
            {
                "type": "ORBITS_NEAR",
                "from": "OrbitalDepot",
                "to": "OrbitalDepot",
                "direction": "UNDIRECTED",
                "managedby": "sync",
            },
        ],
    },
    "sync": {
        "endpoints": [
            {
                "path": "/v1/sync/orbital-depots",
                "targetnode": "OrbitalDepot",
                "idproperty": "depot_id",
                "batchstrategy": "UNWINDMERGE",
            },
            {
                "path": "/v1/sync/stocks",
                "targetedge": "STOCKS",
                "fromnode": "OrbitalDepot",
                "tonode": "PropellantGrade",
                "batchstrategy": "UNWINDMERGE",
            },
            {
                "path": "/v1/sync/orbits-near",
                "targetedge": "ORBITS_NEAR",
                "fromnode": "OrbitalDepot",
                "tonode": "OrbitalDepot",
                "batchstrategy": "UNWINDMERGE",
            },
        ]
    },
}

_CHEESE_SPEC: dict[str, Any] = {
    "domain": {"id": "cave-works", "name": "synthetic", "version": "0.1.0"},
    "ontology": {
        "nodes": [
            {"label": "AgingCave", "properties": [{"name": "cave_id", "required": True}]},
            {"label": "Affineur", "properties": [{"name": "affineur_id", "required": True}]},
        ],
        "edges": [
            {
                "type": "TENDED_BY",
                "from": "AgingCave",
                "to": "Affineur",
                "direction": "DIRECTED",
                "managedby": "sync",
            },
        ],
    },
    "sync": {
        "endpoints": [
            {
                "path": "/v1/sync/aging-caves",
                "targetnode": "AgingCave",
                "idproperty": "cave_id",
                "batchstrategy": "UNWINDMERGE",
            },
            {
                "path": "/v1/sync/tended-by",
                "targetedge": "TENDED_BY",
                "fromnode": "AgingCave",
                "tonode": "Affineur",
                "batchstrategy": "UNWINDMERGE",
            },
        ]
    },
}

VOCABULARIES = {
    "orbit-yard": (_ORBITAL_SPEC, "OrbitalDepot", "STOCKS", "PropellantGrade", "stocks"),
    "cave-works": (_CHEESE_SPEC, "AgingCave", "TENDED_BY", "Affineur", "tended-by"),
}


def _assertion(tenant: str = "orbit-yard", **overrides: Any) -> RelationshipAssertion:
    _spec, subject_label, relation, object_label, _entity_type = VOCABULARIES[tenant]
    fields: dict[str, Any] = {
        "subject_label": subject_label,
        "subject_id": f"{tenant}.subject:1",
        "relation_type": relation,
        "object_label": object_label,
        "object_id": f"{tenant}.object:9",
        "evidence_ref": f"evidence:{tenant}:1",
        "source_ref": "source:document:42",
        "confidence": 0.8,
        "assertion_status": "observed",
        "polarity": "positive",
        "source_revision": "rev-1",
    }
    fields.update(overrides)
    return RelationshipAssertion(**fields)


@pytest.fixture
def router(tmp_path: Path) -> PacketRouter:
    root = tmp_path / "domains"
    for tenant, (spec, *_rest) in VOCABULARIES.items():
        (root / tenant).mkdir(parents=True)
        (root / tenant / "spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    return PacketRouter(gate_url="https://gate-node:8080", domain_reader=DomainYamlReader(root))


def _response(payload: dict[str, Any], tenant: str = "orbit-yard") -> Any:
    return create_transport_packet(
        action="sync",
        payload=payload,
        tenant=tenant,
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )


def _relationship_ack(entity_type: str, created: int = 1, replayed: int = 0) -> dict[str, Any]:
    return {
        "status": "success",
        "entity_type": entity_type,
        "synced_count": 1,
        "created_count": created,
        "replayed_count": replayed,
    }


# ── Contract resolution from the tenant spec ───────────────────────────────────


@pytest.mark.parametrize("tenant", sorted(VOCABULARIES))
def test_relationship_contract_comes_from_the_tenant_spec(tenant: str) -> None:
    spec, subject_label, relation, object_label, entity_type = VOCABULARIES[tenant]
    contract = resolve_relationship_sync_contract(
        spec, relation_type=relation, from_label=subject_label, to_label=object_label
    )
    assert contract is not None
    assert contract.entity_type == entity_type


@pytest.mark.parametrize(
    ("relation", "from_label", "to_label"),
    [
        ("STOCKS", "PropellantGrade", "OrbitalDepot"),  # reversed direction
        ("SUPPLIES", "OrbitalDepot", "PropellantGrade"),  # undeclared type
        ("ORBITS_NEAR", "OrbitalDepot", "OrbitalDepot"),  # declared UNDIRECTED
    ],
)
def test_undeclared_reversed_or_undirected_relationships_resolve_to_nothing(
    relation: str, from_label: str, to_label: str
) -> None:
    assert (
        resolve_relationship_sync_contract(
            _ORBITAL_SPEC, relation_type=relation, from_label=from_label, to_label=to_label
        )
        is None
    )


def test_endpoint_without_matching_ontology_edge_or_with_a_twin_resolves_to_nothing() -> None:
    no_edge = copy.deepcopy(_ORBITAL_SPEC)
    no_edge["ontology"]["edges"] = []
    twin = copy.deepcopy(_ORBITAL_SPEC)
    twin["sync"]["endpoints"].append({**twin["sync"]["endpoints"][1], "path": "/v1/sync/stocks-2"})
    node_and_edge = copy.deepcopy(_ORBITAL_SPEC)
    node_and_edge["sync"]["endpoints"][1]["targetnode"] = "OrbitalDepot"
    for spec in (no_edge, twin, node_and_edge):
        assert (
            resolve_relationship_sync_contract(
                spec, relation_type="STOCKS", from_label="OrbitalDepot", to_label="PropellantGrade"
            )
            is None
        )


def test_edge_endpoints_do_not_change_node_sync_resolution() -> None:
    node_only = copy.deepcopy(_ORBITAL_SPEC)
    node_only["sync"]["endpoints"] = node_only["sync"]["endpoints"][:1]
    for spec in (_ORBITAL_SPEC, node_only):
        contract = resolve_graph_sync_contract(spec)
        assert contract is not None
        assert (contract.entity_type, contract.id_property) == ("orbital-depots", "depot_id")


# ── Assertion admission ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "overrides",
    [
        {"evidence_ref": ""},
        {"source_ref": "  "},
        {"relation_type": ""},
        {"subject_label": ""},
        {"subject_id": ""},
        {"object_id": True},
        {"confidence": None},
        {"confidence": 1.2},
        {"confidence": -0.01},
        {"confidence": float("nan")},
        {"confidence": True},
        {"assertion_status": "guessed"},
        {"polarity": "maybe"},
        {"source_revision": ""},
        {"expires_at": " "},
    ],
)
def test_incomplete_assertions_have_defects(overrides: dict[str, Any]) -> None:
    assert relationship_assertion_defects(_assertion(**overrides))


def test_complete_assertion_has_no_defects() -> None:
    assert relationship_assertion_defects(_assertion()) == []
    assert relationship_assertion_defects(_assertion(source_revision=None, confidence=1)) == []


# ── Acknowledgment ─────────────────────────────────────────────────────────────


def test_only_a_relationship_acknowledgment_confirms_a_relationship() -> None:
    assert relationship_sync_acknowledged(
        _relationship_ack("stocks"), entity_type="stocks", expected_count=1
    )
    assert relationship_sync_acknowledged(
        _relationship_ack("stocks", created=0, replayed=1), entity_type="stocks", expected_count=1
    )
    node_ack = {"status": "success", "entity_type": "stocks", "synced_count": 1}
    for ack in (
        node_ack,
        {**_relationship_ack("stocks"), "entity_type": "orbital-depots"},
        {**_relationship_ack("stocks"), "created_count": 2},
        {**_relationship_ack("stocks"), "created_count": True},
        {**_relationship_ack("stocks"), "status": "all_rejected"},
        None,
    ):
        assert not relationship_sync_acknowledged(ack, entity_type="stocks", expected_count=1)


# ── Sending through Gate ───────────────────────────────────────────────────────


@pytest.mark.parametrize("tenant", sorted(VOCABULARIES))
async def test_relationship_is_sent_as_ceg_sync_through_gate(
    router: PacketRouter, tenant: str
) -> None:
    _spec, _s, _r, _o, entity_type = VOCABULARIES[tenant]
    assertion = _assertion(tenant)
    with patch.object(
        router._client,
        "execute",
        new_callable=AsyncMock,
        return_value=_response(_relationship_ack(entity_type), tenant),
    ) as execute:
        result = await router.notify_relationship_sync(tenant, assertion)
    assert result.outcome is RelationshipSyncOutcome.ACKNOWLEDGED
    assert result.acknowledged
    assert result.ack is not None and result.ack["created_count"] == 1
    kwargs = execute.await_args.kwargs
    assert kwargs["action"] == "sync"
    assert kwargs["tenant"] == tenant
    assert kwargs["payload"] == {
        "entity_type": entity_type,
        "batch": [
            {
                "from_id": assertion.subject_id,
                "to_id": assertion.object_id,
                "evidence_ref": assertion.evidence_ref,
                "source_ref": "source:document:42",
                "confidence": 0.8,
                "assertion_status": "observed",
                "polarity": "positive",
                "source_revision": "rev-1",
            }
        ],
    }
    assert kwargs["idempotency_key"].startswith(f"eie:relsync:{tenant}:{assertion.evidence_ref}:")


async def test_retry_reuses_the_pinned_key_and_a_different_assertion_does_not(
    router: PacketRouter,
) -> None:
    keys = []
    for assertion in (_assertion(), _assertion(), _assertion(confidence=0.3)):
        with patch.object(
            router._client,
            "execute",
            new_callable=AsyncMock,
            return_value=_response(_relationship_ack("stocks")),
        ) as execute:
            await router.notify_relationship_sync("orbit-yard", assertion)
        keys.append(execute.await_args.kwargs["idempotency_key"])
    assert keys[0] == keys[1]
    assert keys[2] != keys[0]


def test_idempotency_key_binds_tenant_and_every_structural_contract_field() -> None:
    contract = RelationshipSyncContract(
        entity_type="stocks",
        relation_type="STOCKS",
        from_label="OrbitalDepot",
        to_label="PropellantGrade",
    )
    row = {"from_id": "d:1", "to_id": "g:1", "evidence_ref": "e:1", "source_ref": "s:1"}
    key = _relationship_idempotency_key("orbit-yard", contract, row)
    assert key == _relationship_idempotency_key(
        "orbit-yard", contract, dict(row)
    )  # exact retry is stable
    assert key.startswith("eie:relsync:orbit-yard:e:1:")
    variants = [
        _relationship_idempotency_key("other-yard", contract, row),
        _relationship_idempotency_key(
            "orbit-yard", dataclasses.replace(contract, entity_type="stocks-v2"), row
        ),
        _relationship_idempotency_key(
            "orbit-yard", dataclasses.replace(contract, relation_type="SUPPLIES"), row
        ),
        _relationship_idempotency_key(
            "orbit-yard", dataclasses.replace(contract, from_label="Depot"), row
        ),
        _relationship_idempotency_key(
            "orbit-yard", dataclasses.replace(contract, to_label="Grade"), row
        ),
        _relationship_idempotency_key("orbit-yard", contract, {**row, "to_id": "g:2"}),
    ]
    digests = {k.rsplit(":", 1)[1] for k in [key, *variants]}
    assert len(digests) == len(variants) + 1


@pytest.mark.parametrize(
    "assertion",
    [
        _assertion(confidence=None),
        _assertion(relation_type="SUPPLIES"),
        _assertion(subject_label="PropellantGrade", object_label="OrbitalDepot"),
    ],
    ids=["incomplete", "undeclared", "reversed"],
)
async def test_unsendable_assertions_never_reach_gate(
    router: PacketRouter, assertion: RelationshipAssertion
) -> None:
    with patch.object(router._client, "execute", new_callable=AsyncMock) as execute:
        result = await router.notify_relationship_sync("orbit-yard", assertion)
    assert result.outcome is RelationshipSyncOutcome.NOT_PROPOSED
    assert result.ack is None and not result.acknowledged
    execute.assert_not_awaited()


async def test_unknown_tenant_is_skipped(router: PacketRouter) -> None:
    with patch.object(router._client, "execute", new_callable=AsyncMock) as execute:
        result = await router.notify_relationship_sync("no-such-tenant", _assertion())
    assert result.outcome is RelationshipSyncOutcome.NOT_PROPOSED
    execute.assert_not_awaited()


def _failure_packet(*, with_hop: bool) -> Any:
    """What Gate relays (HTTP 200) when the graph worker's handler raised.

    Gate_SDK ``create_error_transport_packet`` shape. The error name inside the
    payload is the worker's own and is deliberately NOT read by EIE: the
    outcome below is decided by the ``failure`` packet type alone.
    """
    request = _response(_relationship_ack("stocks"))
    failure = request.derive(
        packet_type="failure",
        source_node="graph-engine",
        destination_node="enrichment-engine",
        reply_to="graph-engine",
        payload={"status": "failed", "error": "AnyWorkerError", "message": "refused"},
    )
    if with_hop:
        failure = failure.with_hop(
            make_response_hop(
                packet=failure,
                node="graph-engine",
                action="sync",
                status="failed",
                error_code="AnyWorkerError",
            )
        )
    return failure


# ── Typed outcome: transport unavailable vs not acknowledged (GAR A5) ──────────


@pytest.mark.parametrize(
    "error",
    [
        GateConnectionError("gate down"),
        GateTimeoutError("deadline elapsed", timeout_seconds=10.0),
        GateHTTPError("gate overloaded", status_code=503),
        GateHTTPError("bad gateway", status_code=502),
    ],
    ids=["connection", "timeout", "5xx-503", "5xx-502"],
)
async def test_gate_outage_after_retries_is_transport_unavailable(
    router: PacketRouter, error: Exception
) -> None:
    """Gate could not carry the row: retried under the pinned key, then reported as an outage."""
    with (
        patch.object(
            router._client, "execute", new_callable=AsyncMock, side_effect=error
        ) as execute,
        patch("app.engines.packet_router.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await router.notify_relationship_sync("orbit-yard", _assertion())
    assert isinstance(result, RelationshipSyncResult)
    assert result.outcome is RelationshipSyncOutcome.TRANSPORT_UNAVAILABLE
    assert result.ack is None and not result.acknowledged
    # Retries are spent before the outage is reported (the key makes them safe).
    assert execute.await_count == 2


@pytest.mark.parametrize(
    "error",
    [
        GateHTTPError("unprocessable", status_code=422),
        GateHTTPError("no route", status_code=404),
        GateAuthorizationError("action not permitted", status_code=403),
    ],
    ids=["4xx-422", "4xx-404", "4xx-403"],
)
async def test_gate_4xx_is_not_acknowledged_and_not_retried(
    router: PacketRouter, error: Exception
) -> None:
    with (
        patch.object(
            router._client, "execute", new_callable=AsyncMock, side_effect=error
        ) as execute,
        patch("app.engines.packet_router.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await router.notify_relationship_sync("orbit-yard", _assertion())
    assert result.outcome is RelationshipSyncOutcome.NOT_ACKNOWLEDGED
    assert result.ack is None
    assert execute.await_count == 1


@pytest.mark.parametrize("with_hop", [False, True], ids=["failure-type", "failure-type+hop"])
async def test_worker_failure_packet_is_not_acknowledged(
    router: PacketRouter, with_hop: bool
) -> None:
    with patch.object(
        router._client,
        "execute",
        new_callable=AsyncMock,
        return_value=_failure_packet(with_hop=with_hop),
    ) as execute:
        result = await router.notify_relationship_sync("orbit-yard", _assertion())
    assert result.outcome is RelationshipSyncOutcome.NOT_ACKNOWLEDGED
    assert result.ack is None
    assert execute.await_count == 1


@pytest.mark.parametrize(
    "answer",
    [
        {"status": "success", "entity_type": "stocks", "synced_count": 1},
        {"status": "all_rejected", "entity_type": "stocks", "synced_count": 0},
        _relationship_ack("tended-by"),
        _relationship_ack("stocks", created=0, replayed=0),
    ],
    ids=["node-shaped", "all-rejected", "other-endpoint", "zero-rows"],
)
async def test_unconfirming_answers_are_not_acknowledged(
    router: PacketRouter, answer: dict[str, Any]
) -> None:
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_response(answer)
    ):
        result = await router.notify_relationship_sync("orbit-yard", _assertion())
    assert result.outcome is RelationshipSyncOutcome.NOT_ACKNOWLEDGED
    assert result.ack is None


def test_outcome_is_decided_on_transport_signals_only() -> None:
    """EIE never couples to the graph worker's exception vocabulary (GAR A5)."""
    source = Path("app/engines/packet_router.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {"RelationshipSyncOutcome", "relationship_failure_outcome", "_send_relationship"}
    segments = [
        ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name in names
    ]
    assert len(segments) == len(names)
    classifier = "\n".join(segments)
    for worker_vocabulary in ("ValidationError", "ExecutionError", "payload"):
        assert worker_vocabulary not in classifier


def test_assertion_is_immutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        _assertion().confidence = 1.0  # type: ignore[misc]


def test_relationship_sender_names_no_product_domain() -> None:
    source = Path("app/engines/packet_router.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    start = next(
        node.lineno
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RelationshipAssertion"
    )
    section = "\n".join(source.splitlines()[start - 1 :]).lower()
    for name in ("plasticos", "hubspot", "polymer", "recycl"):
        assert name not in section
