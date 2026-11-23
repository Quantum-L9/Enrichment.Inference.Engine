"""Generic spec-driven graph projection witnesses (campaign EIE-NATIVE-CLOSURE-001, E1/E5).

Graph rows are built from the tenant spec only: the target node's properties
that are not ``evidenceonly`` plus ``sync.endpoints[].childsync``, projected by
``packet_router.project_graph_sync_row``. Every input here is already in the
declared wire shape, so ``polymer_capability`` (the PlasticOS-only shaper) is
never involved. Each case runs on a domain-neutral synthetic cartridge and on
PlasticOS as a regression witness.

Live CEG / Gate effects (an independent graph read, MERGE idempotency on the
graph, retry ownership after CEG failure) are out of reach here and belong to
the peer campaign (A1 / A2 / A8).

L9_META:
  tier: 2
  domain: graph-sync
  authority: EIE-NATIVE-CLOSURE-001 lane L3
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from constellation_node_sdk.transport import create_transport_packet

from app.engines.domain_yaml_reader import DomainYamlReader
from app.engines.handlers import admitted_fields
from app.engines.packet_router import (
    GraphSyncContract,
    PacketRouter,
    _sync_idempotency_key,
    _usable_sync_endpoints,
    graph_sync_acknowledged,
    project_graph_sync_row,
    resolve_graph_sync_contract,
)
from app.engines.polymer_capability import shape_graph_fields
from app.engines.spec_types import node_properties, row_properties

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
DOMAINS = ROOT / "domains"
# The revision current.json publishes, through the loader runtime uses (never the sample).
PLASTICOS = DomainYamlReader(DOMAINS, require_published=True).load("plasticos").raw_spec
CEG_SNAPSHOT = ROOT / "tests/fixtures/ceg/plasticos.spec.yaml"
GATE = "http://gate.test"

# A cartridge that shares nothing with PlasticOS but the grammar.
NEUTRAL: dict[str, Any] = {
    "name": "neutral",
    "version": "1.0.0",
    "ontology": {
        "nodes": [
            {
                "label": "Site",
                "properties": [
                    {"name": "site_id", "type": "string", "required": True},
                    {"name": "title", "type": "string"},
                    {"name": "rating", "type": "float"},
                    {
                        "name": "handled_items",
                        "type": "list",
                        "items": {
                            "type": "map",
                            "properties": [
                                {"name": "key", "type": "string", "required": True},
                                {"name": "slot", "type": "string"},
                                {"name": "mode", "type": "string"},
                            ],
                        },
                    },
                    {"name": "notes", "type": "string", "evidenceonly": True},
                ],
            },
            {"label": "Item", "properties": [{"name": "key", "type": "string"}]},
        ]
    },
    "sync": {
        "endpoints": [
            {
                "path": "/v1/sync/sites",
                "targetnode": "Site",
                "idproperty": "site_id",
                "batchstrategy": "UNWINDMERGE",
                "childsync": [
                    {
                        "field": "handled_items",
                        "targetnode": "Item",
                        "targetid": "key",
                        "edgetype": "HANDLES",
                        "edgedirection": "parenttochild",
                    }
                ],
            }
        ]
    },
}


class Case:
    """One cartridge's names for the same structural roles."""

    def __init__(
        self,
        spec: dict[str, Any],
        *,
        label: str,
        entity_type: str,
        id_property: str,
        child_field: str,
        child_id: str,
        child_line: str,
        child_dir: str,
        evidence_field: str,
        scalar_field: str,
    ) -> None:
        self.spec = spec
        self.label = label
        self.entity_type = entity_type
        self.id_property = id_property
        self.child_field = child_field
        self.child_id = child_id
        self.child_line = child_line
        self.child_dir = child_dir
        self.evidence_field = evidence_field
        self.scalar_field = scalar_field

    def contract(self) -> GraphSyncContract:
        contract = resolve_graph_sync_contract(self.spec)
        assert contract is not None
        return contract

    def child(self, ident: str, line: str | None = None, **extra: Any) -> dict[str, Any]:
        element: dict[str, Any] = {self.child_id: ident}
        if line is not None:
            element[self.child_line] = line
        element.update(extra)
        return element


CASES = {
    "neutral": Case(
        NEUTRAL,
        label="Site",
        entity_type="sites",
        id_property="site_id",
        child_field="handled_items",
        child_id="key",
        child_line="slot",
        child_dir="mode",
        evidence_field="notes",
        scalar_field="rating",
    ),
    "plasticos": Case(
        PLASTICOS,
        label="Facility",
        entity_type="facilities",
        id_property="facility_id",
        child_field="processed_polymers",
        child_id="code",
        child_line="line_id",
        child_dir="direction",
        evidence_field="city",
        scalar_field="contamination_tolerance",
    ),
}


@pytest.fixture(params=sorted(CASES))
def case(request: pytest.FixtureRequest) -> Case:
    return CASES[request.param]


def _project(case: Case, fields: dict[str, Any], entity_id: str = "E-1") -> Any:
    return project_graph_sync_row(entity_id=entity_id, fields=fields, contract=case.contract())


def _ceg() -> dict[str, Any]:
    body = CEG_SNAPSHOT.read_text(encoding="utf-8").split("--- end header ---", 1)[1]
    loaded = yaml.safe_load(body)
    assert isinstance(loaded, dict)
    return loaded


def _ack(payload: dict[str, Any]) -> object:
    return create_transport_packet(
        action="sync",
        payload=payload,
        tenant="plasticos",
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )


def _reader(tmp_path: Path, specs: dict[str, dict[str, Any]]) -> DomainYamlReader:
    root = tmp_path / "domains"
    for tenant, spec in specs.items():
        (root / tenant).mkdir(parents=True)
        (root / tenant / "spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    return DomainYamlReader(root)


# ── E1: effective revision and declared graph surface ───────────────────────


def test_s01_s02_published_pointer_is_the_only_accepted_revision(tmp_path: Path) -> None:
    pointer = json.loads((DOMAINS / "plasticos/current.json").read_text(encoding="utf-8"))
    revision = DOMAINS / "plasticos/revisions" / pointer["sha256"]
    receipt = json.loads((revision / "receipt.json").read_text(encoding="utf-8"))
    revision_bytes = (revision / "spec.yaml").read_bytes()
    assert hashlib.sha256(revision_bytes).hexdigest() == pointer["sha256"] == receipt["sha256"]
    assert receipt["reviewed_input_sha256"] == pointer["sha256"]

    loaded = DomainYamlReader(DOMAINS, require_published=True).load("plasticos")
    assert loaded.source_sha256 == pointer["sha256"]
    assert loaded.version == receipt["version"] == yaml.safe_load(revision_bytes)["version"]

    # A sample spec.yaml beside the pointer must not shadow the reviewed revision.
    copy = tmp_path / "domains"
    shutil.copytree(DOMAINS / "plasticos", copy / "plasticos")
    (copy / "plasticos/spec.yaml").write_text(
        "name: plasticos\nversion: '99.0.0'\nontology: {nodes: []}\n", encoding="utf-8"
    )
    shadowed = DomainYamlReader(copy, require_published=True).load("plasticos")
    assert shadowed.source_sha256 == pointer["sha256"]
    assert shadowed.version == receipt["version"]


def test_s03_plasticos_declares_exactly_facility_and_processes_childsync() -> None:
    endpoints = PLASTICOS["sync"]["endpoints"]
    assert [(e["path"], e["targetnode"], e["idproperty"]) for e in endpoints] == [
        ("/v1/sync/facilities", "Facility", "facility_id")
    ]
    assert [
        (c["field"], c["targetnode"], c["targetid"], c["edgetype"])
        for c in endpoints[0]["childsync"]
    ] == [("processed_polymers", "PolymerFamily", "code", "PROCESSES")]
    assert _usable_sync_endpoints(PLASTICOS) == [("facilities", "facility_id", "Facility")]


def test_s04_every_evidenceonly_property_is_off_the_row_schema(case: Case) -> None:
    props = node_properties(case.spec, case.label)
    schema = row_properties(case.spec, case.label)
    assert props is not None and schema is not None
    evidence = {p["name"] for p in props if p.get("evidenceonly") is True}
    assert case.evidence_field in evidence
    assert evidence.isdisjoint(schema)
    assert set(schema) == {p["name"] for p in props} - evidence


def test_s05_ceg_owned_facility_fields_are_not_claimed_writable() -> None:
    ceg_facility = next(n for n in _ceg()["ontology"]["nodes"] if n["label"] == "Facility")
    ceg_names = {p["name"] for p in ceg_facility["properties"]}
    schema = row_properties(PLASTICOS, "Facility")
    assert schema is not None
    owned_by_ceg = ceg_names - set(schema)
    assert {"entity_ref", "lat", "lon", "credit_score", "community_id"} <= owned_by_ceg
    declared = {p["name"] for p in node_properties(PLASTICOS, "Facility") or []}
    assert owned_by_ceg.isdisjoint(declared)
    ceg_owned_subjects = {
        name for subject in PLASTICOS["subjects"]["types"] for name in subject["ceg_owned"]
    }
    assert ceg_owned_subjects == owned_by_ceg
    assert set(schema) <= ceg_names  # nothing EIE sends is unknown to CEG


def test_s06_ceg_labels_without_an_eie_endpoint_are_not_produced() -> None:
    ceg_labels = {n["label"] for n in _ceg()["ontology"]["nodes"]}
    produced = {t for _, _, t in _usable_sync_endpoints(PLASTICOS)}
    children = {c["targetnode"] for c in PLASTICOS["sync"]["endpoints"][0]["childsync"]}
    not_produced = ceg_labels - produced - children
    assert not_produced == {"MaterialIntake", "MaterialForm", "SupplyOpportunity", "BuyerDemand"}
    for label in not_produced:
        contract = resolve_graph_sync_contract(PLASTICOS, node_label=label)
        assert contract is not None
        assert contract.entity_type == "facilities"


def test_s08_both_plasticos_subject_types_bind_facility() -> None:
    subjects = PLASTICOS["subjects"]
    assert subjects["roles"] == ["supplier", "buyer"]
    assert {t["name"]: t["node"] for t in subjects["types"]} == {
        "facility": "Facility",
        "material": "Facility",
    }
    assert [n["label"] for n in PLASTICOS["ontology"]["nodes"]] == ["Facility"]


def test_s10_two_endpoints_and_no_selection_guess_nothing() -> None:
    spec = yaml.safe_load(yaml.safe_dump(NEUTRAL))
    spec["sync"]["endpoints"].append(
        {"path": "/v1/sync/items", "targetnode": "Item", "idproperty": "key"}
    )
    assert resolve_graph_sync_contract(spec) is None
    assert resolve_graph_sync_contract(spec, node_label="Elsewhere") is None
    chosen = resolve_graph_sync_contract(spec, node_label="Site")
    assert chosen is not None and chosen.entity_type == "sites"


def test_s11_g08_g15_undeclared_evidenceonly_and_ceg_owned_fields_are_dropped(
    case: Case,
) -> None:
    fields = {
        case.scalar_field: 2.5,
        case.evidence_field: "evidence",
        "community_id": 7,
        "score": 0.91,
        "credit_score": 700,
        "surprise": "x",
    }
    projection = _project(case, fields)
    assert projection.row == {case.id_property: "E-1", case.scalar_field: 2.5}
    assert set(projection.dropped) == {
        case.evidence_field,
        "community_id",
        "score",
        "credit_score",
        "surprise",
    }


def test_s12_converge_path_omits_null_so_the_graph_is_not_cleared(case: Case) -> None:
    result = {"fields": {case.scalar_field: None, case.child_field: None, "title": None}}
    fields = admitted_fields(result)
    assert fields == {}
    assert _project(case, fields).row == {case.id_property: "E-1"}


def test_s12_direct_projection_still_sends_null_as_a_clear_for_other_callers(
    case: Case,
) -> None:
    # Characterization: only the converge path (admitted_fields) drops nulls.
    row = _project(case, {case.scalar_field: None}).row
    assert row == {case.id_property: "E-1", case.scalar_field: None}


def test_s13_g19_neutral_cartridge_gets_no_plasticos_shaping_or_defaults() -> None:
    odoo_shaped = [{"polymer": "HDPE", "capability_id": 411}]
    fields = {"processed_polymers": odoo_shaped, "handled_items": [{"key": "A1"}]}
    assert shape_graph_fields("neutral", fields) is fields
    projection = _project(CASES["neutral"], fields)
    assert projection.row == {"site_id": "E-1", "handled_items": [{"key": "A1"}]}
    assert projection.dropped == ("processed_polymers",)


# ── E5: childsync rows, already in wire shape ───────────────────────────────


def test_g01_one_line_is_one_row_and_one_child(case: Case) -> None:
    row = _project(case, {case.child_field: [case.child("HDPE", "411")]}).row
    assert row == {case.id_property: "E-1", case.child_field: [case.child("HDPE", "411")]}


def test_g02_two_lines_keep_distinct_line_identities(case: Case) -> None:
    lines = [case.child("HDPE", "411"), case.child("HDPE", "412")]
    row = _project(case, {case.child_field: lines}).row
    assert row is not None
    children = row[case.child_field]
    assert [c[case.child_id] for c in children] == ["HDPE", "HDPE"]
    assert [c[case.child_line] for c in children] == ["411", "412"]


def test_g03_directions_are_retained_without_inversion(case: Case) -> None:
    lines = [
        case.child("HDPE", "1", **{case.child_dir: "in"}),
        case.child("PP", "2", **{case.child_dir: "out"}),
    ]
    row = _project(case, {case.child_field: lines}).row
    assert row is not None
    assert [(c[case.child_id], c[case.child_dir]) for c in row[case.child_field]] == [
        ("HDPE", "in"),
        ("PP", "out"),
    ]


def test_g04_child_without_a_line_id_gets_no_invented_identity(case: Case) -> None:
    row = _project(case, {case.child_field: [case.child("HDPE")]}).row
    assert row is not None
    (child,) = row[case.child_field]
    assert case.child_line not in child
    # Characterization (finding F19/G04): the declared line key is optional, so the
    # child still rides the row without edge identity; refusal is not implemented.
    assert child == {case.child_id: "HDPE"}


def test_valid_children_keep_every_declared_key_and_only_those(case: Case) -> None:
    child = case.child("HDPE", "411", **{case.child_dir: "in", "undeclared": "x"})
    row = _project(case, {case.child_field: [child]}).row
    assert row is not None
    assert row[case.child_field] == [case.child("HDPE", "411", **{case.child_dir: "in"})]


def test_missing_required_child_key_refuses_the_whole_row(case: Case) -> None:
    projection = _project(
        case,
        {
            case.scalar_field: 1.0,
            case.child_field: [case.child("HDPE", "1"), {case.child_line: "2"}],
        },
    )
    assert projection.row is None
    assert [d.field for d in projection.defects] == [f"{case.child_field}[1].{case.child_id}"]


def test_null_child_element_refuses_the_whole_row(case: Case) -> None:
    projection = _project(case, {case.child_field: [None]})
    assert projection.row is None
    assert projection.defects[0].reason == "null item"


def test_g07_wire_shaped_input_is_an_identity_projection(case: Case) -> None:
    wire = [case.child("HDPE", "411", **{case.child_dir: "in"})]
    first = _project(case, {case.child_field: wire}).row
    assert first is not None
    second = _project(case, {k: v for k, v in first.items() if k != case.id_property}).row
    assert first == second
    assert first[case.child_field] == wire
    assert shape_graph_fields(case.spec["name"], {case.child_field: wire})[case.child_field] == wire


@pytest.mark.parametrize(
    ("value", "where", "actual"),
    [
        (["HDPE", "PP"], "[0]", "string"),
        ('[{"code": "HDPE", "line_id": "411"}]', "", "string"),
        ({"code": "HDPE"}, "", "map"),
        ([{"code": 5}], "[0].code", "int"),
    ],
)
def test_g09_untyped_or_encoded_child_values_refuse_the_whole_row(
    case: Case, value: Any, where: str, actual: str
) -> None:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        value = [{case.child_id: value[0]["code"]}]
    projection = _project(case, {case.scalar_field: 3.0, case.child_field: value})
    assert projection.row is None
    defect = projection.defects[0]
    assert len(projection.defects) == (len(value) if isinstance(value, list) else 1)
    assert defect.field == f"{case.child_field}{where.replace('.code', '.' + case.child_id)}"
    assert defect.actual == actual
    assert defect.declared.startswith(("list<map{", "map{", "string"))


def test_duplicate_child_lines_are_sent_as_given_not_merged_or_synthesized(case: Case) -> None:
    line = case.child("HDPE", "411")
    row = _project(case, {case.child_field: [line, dict(line)]}).row
    assert row is not None
    # Characterization: EIE neither deduplicates nor refuses; edge identity is CEG's MERGE.
    assert row[case.child_field] == [line, line]


def test_g12_replayed_row_carries_the_same_idempotency_key(case: Case) -> None:
    fields = {case.child_field: [case.child("HDPE", "411")]}
    first = _project(case, fields).row
    again = _project(case, json.loads(json.dumps(fields))).row
    assert first == again and first is not None
    assert _sync_idempotency_key("t", "E-1", first) == _sync_idempotency_key("t", "E-1", again)
    changed = _project(case, {case.child_field: [case.child("HDPE", "412")]}).row
    assert changed is not None
    assert _sync_idempotency_key("t", "E-1", changed) != _sync_idempotency_key("t", "E-1", first)


# ── Router: tenant scoping, node surface, acknowledgment ────────────────────


@pytest.mark.asyncio
async def test_g13_foreign_tenant_resolves_no_contract_and_sends_nothing(tmp_path: Path) -> None:
    router = PacketRouter(gate_url=GATE, domain_reader=_reader(tmp_path, {"plasticos": PLASTICOS}))
    with patch.object(router._client, "execute", new_callable=AsyncMock) as execute:
        result = await router.notify_graph_sync(
            tenant_id="intruder",
            entity_id="plasticos-facility-1",
            fields={"processed_polymers": [{"code": "HDPE", "line_id": "411"}]},
            domain="plasticos",
        )
    assert result is None
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_g13_g14_sync_is_scoped_to_the_callers_tenant_and_one_endpoint(
    tmp_path: Path,
) -> None:
    router = PacketRouter(
        gate_url=GATE, domain_reader=_reader(tmp_path, {"plasticos": PLASTICOS, "neutral": NEUTRAL})
    )
    ok = {"status": "success", "entity_type": "facilities", "synced_count": 1}
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_ack(ok)
    ) as execute:
        result = await router.notify_graph_sync(
            tenant_id="plasticos",
            entity_id="F-1",
            fields={"processed_polymers": [{"code": "HDPE", "line_id": "411"}], "city": "X"},
            domain="neutral",
            node_label="PolymerFamily",
        )
    assert result is not None
    assert (result["status"], result["synced_count"]) == ("success", 1)
    assert execute.await_args is not None
    kwargs = execute.await_args.kwargs
    assert kwargs["tenant"] == "plasticos"
    assert kwargs["payload"] == {
        "entity_type": "facilities",
        "batch": [
            {"facility_id": "F-1", "processed_polymers": [{"code": "HDPE", "line_id": "411"}]}
        ],
    }


@pytest.mark.asyncio
async def test_g19_second_tenant_projects_by_its_own_spec(tmp_path: Path) -> None:
    router = PacketRouter(
        gate_url=GATE, domain_reader=_reader(tmp_path, {"plasticos": PLASTICOS, "neutral": NEUTRAL})
    )
    ok = {"status": "success", "entity_type": "sites", "synced_count": 1}
    fields = {"processed_polymers": [{"polymer": "HDPE", "capability_id": 411}], "title": "S"}
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_ack(ok)
    ) as execute:
        await router.notify_graph_sync(tenant_id="neutral", entity_id="S-1", fields=fields)
    assert execute.await_args is not None
    assert execute.await_args.kwargs["tenant"] == "neutral"
    assert execute.await_args.kwargs["payload"] == {
        "entity_type": "sites",
        "batch": [{"site_id": "S-1", "title": "S"}],
    }


@pytest.mark.parametrize(
    "ack",
    [
        None,
        "success",
        {},
        {"status": "success"},
        {"status": "success", "entity_type": "facilities", "synced_count": 0},
        {"status": "success", "entity_type": "facilities", "synced_count": 2},
        {"status": "success", "entity_type": "facilities", "synced_count": True},
        {"status": "success", "entity_type": "facilities", "synced_count": "1"},
        {"status": "success", "entity_type": "polymerfamilies", "synced_count": 1},
        {"status": "all_rejected", "entity_type": "facilities", "synced_count": 0},
        {"status": "partial", "entity_type": "facilities", "synced_count": 1},
    ],
)
def test_g18_malformed_or_miscounted_ack_is_not_confirmation(ack: Any) -> None:
    assert not graph_sync_acknowledged(ack, entity_type="facilities", expected_count=1)


def test_g18_exact_ack_is_confirmation() -> None:
    ack = {"status": "success", "entity_type": "facilities", "synced_count": 1}
    assert graph_sync_acknowledged(ack, entity_type="facilities", expected_count=1)


@pytest.mark.asyncio
async def test_g18_router_returns_none_on_a_miscounted_ack(tmp_path: Path) -> None:
    router = PacketRouter(gate_url=GATE, domain_reader=_reader(tmp_path, {"plasticos": PLASTICOS}))
    bad = {"status": "success", "entity_type": "facilities", "synced_count": 3}
    with patch.object(router._client, "execute", new_callable=AsyncMock, return_value=_ack(bad)):
        result = await router.notify_graph_sync(
            tenant_id="plasticos", entity_id="F-1", fields={"name": "N"}
        )
    assert result is None
