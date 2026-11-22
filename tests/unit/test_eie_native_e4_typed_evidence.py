"""E4 typed evidence: a declared value keeps its declared shape until the graph row.

Campaign EIE-NATIVE-CLOSURE-001, lane L2, findings F04 / F05 / F22 (and the
acceptance cases T01-T18 the lane carries). Every case runs against a
domain-neutral synthetic cartridge (``synthdomain``, node ``Widget``) so EIE
core is proven without PlasticOS; PlasticOS stays a regression witness in
``tests/unit/test_typed_spec_contract.py``.

Owners exercised, earliest first:

* ``converge_request_v2`` -- v2 request resolution (read-only here);
* ``validation_engine.validate_response`` -- provider reply -> typed fields;
* ``handlers.admitted_fields`` -- what a converge result may hand to CEG;
* ``packet_router.project_graph_sync_row`` / ``notify_graph_sync`` -- the
  typed sync row, which fails closed before anything is sent.

L9_META:
  tier: 2
  domain: graph-sync
  authority: EIE-NATIVE-CLOSURE-001 lane L2 (slice E4)
  pr_class: app_code + tier2_test
"""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import structlog
import yaml
from constellation_node_sdk.transport import create_transport_packet

from app.engines.converge_contract import restrict_fields_to_schema
from app.engines.converge_request_v2 import (
    REQUEST_CONTRACT_V2,
    ConvergeRequestRefusedError,
    field_declarations,
    resolve_converge_request_v2,
)
from app.engines.convergence.observation_evidence import observation_evidence
from app.engines.domain_yaml_reader import DomainYamlReader
from app.engines.handlers import admitted_fields
from app.engines.packet_router import (
    PacketRouter,
    graph_sync_acknowledged,
    project_graph_sync_row,
)
from app.engines.spec_types import shape
from app.services.validation_engine import validate_response

pytestmark = pytest.mark.unit

TENANT = "synthdomain"
NODE = "Widget"
GATE = "https://gate-node:8080"

MEMBERS = {
    "type": "list",
    "items": {
        "type": "map",
        "properties": [
            {"name": "code", "type": "string", "required": True},
            {"name": "line_id", "type": "string"},
            {"name": "direction", "type": "string"},
        ],
    },
}

SPEC: dict[str, Any] = {
    "name": TENANT,
    "version": "1.0.0",
    "description": "Domain-neutral synthetic cartridge for typed-evidence witnesses.",
    "ontology": {
        "nodes": [
            {
                "label": NODE,
                "properties": [
                    {"name": "widget_id", "type": "string", "required": True},
                    {"name": "members", **MEMBERS},
                    {"name": "weight", "type": "float"},
                    {"name": "units", "type": "int"},
                    {"name": "tags", "type": "list", "items": {"type": "string"}},
                    {"name": "label", "type": "string"},
                    {"name": "grade", "type": "string", "evidenceonly": True},
                ],
            }
        ]
    },
    "sync": {
        "endpoints": [
            {
                "path": "/v1/sync/widgets",
                "targetnode": NODE,
                "idproperty": "widget_id",
                "batchstrategy": "UNWINDMERGE",
                "childsync": [
                    {
                        "field": "members",
                        "targetnode": "Part",
                        "targetid": "code",
                        "edgetype": "HAS_PART",
                        "edgedirection": "parenttochild",
                    }
                ],
            }
        ]
    },
}

FULL_SCHEMA: dict[str, Any] = {"members": MEMBERS, "weight": "float", "units": "int"}


@pytest.fixture
def reader(tmp_path: Path) -> DomainYamlReader:
    (tmp_path / TENANT).mkdir()
    (tmp_path / TENANT / "spec.yaml").write_text(yaml.safe_dump(SPEC), encoding="utf-8")
    return DomainYamlReader(tmp_path)


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "contract": REQUEST_CONTRACT_V2,
        "cartridge": {"id": TENANT, "version": SPEC["version"]},
        "node": NODE,
        "targets": [{"field": "members", "type": MEMBERS}, {"field": "weight"}],
        "entity": {"id": "widget:1", "name": "Synthetic Widget"},
        "object_type": "widget",
        "objective": "Enrich the widget profile",
    }
    body.update(overrides)
    return body


def _contract(reader: DomainYamlReader) -> Any:
    contract = PacketRouter(gate_url=GATE, domain_reader=reader).graph_sync_contract(
        TENANT, node_label=NODE
    )
    assert contract is not None
    return contract


def _ack() -> object:
    return create_transport_packet(
        action="sync",
        payload={"status": "success", "entity_type": "widgets", "synced_count": 1},
        tenant=TENANT,
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )


async def _sync(reader: DomainYamlReader, fields: dict[str, Any]) -> tuple[Any, AsyncMock, list]:
    router = PacketRouter(gate_url=GATE, domain_reader=reader)
    with (
        structlog.testing.capture_logs() as logs,
        patch.object(
            router._client, "execute", new_callable=AsyncMock, return_value=_ack()
        ) as execute,
    ):
        result = await router.notify_graph_sync(
            tenant_id=TENANT,
            entity_id="widget:1",
            fields=copy.deepcopy(fields),
            domain=TENANT,
            node_label=NODE,
        )
    return result, execute, logs


def _validated(fields: dict[str, Any], schema: Any) -> dict[str, Any]:
    return validate_response({"fields": fields, "confidence": 0.9}, schema)


# ── T02 [F04] the external v2 request/response shape is unchanged ─────────────


def test_t02_v2_request_resolves_and_response_declaration_is_the_typed_shape(
    reader: DomainYamlReader,
) -> None:
    resolved = resolve_converge_request_v2(TENANT, _body(), reader)
    assert resolved is not None
    # The caller's typed expectation is checked against the cartridge, not echoed.
    assert set(resolved.body["schema"]) == {"members", "weight"}
    assert field_declarations(resolved)["members"] == shape({"name": "members", **MEMBERS})
    # Omitting `type` is the same request; a wrong `type` is refused.
    assert (
        resolve_converge_request_v2(TENANT, _body(targets=[{"field": "members"}]), reader)
        is not None
    )
    with pytest.raises(ConvergeRequestRefusedError) as refused:
        resolve_converge_request_v2(
            TENANT,
            _body(
                targets=[
                    {"field": "members", "type": {"type": "list", "items": {"type": "string"}}}
                ]
            ),
            reader,
        )
    assert refused.value.code == "target_type_mismatch"


# ── T01/T03/T04 [F04] a declared list<map> keeps its declared members ─────────


def test_t01_t03_declared_list_of_maps_is_validated_by_its_declaration() -> None:
    result = _validated(
        {"members": [{"code": "A1", "line_id": "L1", "direction": "in", "undeclared": 9}]},
        FULL_SCHEMA,
    )
    # Declared keys only, each a real mapping, never a "{'code': ...}" string.
    assert result["members"] == [{"code": "A1", "line_id": "L1", "direction": "in"}]
    assert all(isinstance(member, dict) for member in result["members"])


def test_t03_t04_row_keeps_declared_keys_and_both_line_ids_in_order(
    reader: DomainYamlReader,
) -> None:
    members = [
        {"code": "A1", "line_id": "L2", "direction": "out", "undeclared": 1},
        {"code": "A1", "line_id": "L1", "direction": "in"},
    ]
    validated = _validated({"members": members}, FULL_SCHEMA)
    assert [m["line_id"] for m in validated["members"]] == ["L2", "L1"]
    projection = project_graph_sync_row(
        entity_id="widget:1", fields={"members": validated["members"]}, contract=_contract(reader)
    )
    assert projection.defects == ()
    assert projection.row == {
        "widget_id": "widget:1",
        "members": [
            {"code": "A1", "line_id": "L2", "direction": "out"},
            {"code": "A1", "line_id": "L1", "direction": "in"},
        ],
    }


# ── T05 [F04] plain strings are not a list<map> ──────────────────────────────


@pytest.mark.asyncio
async def test_t05_plain_strings_for_a_list_of_maps_are_refused_before_sync(
    reader: DomainYamlReader,
) -> None:
    assert "members" not in _validated({"members": ["A1", "B2"]}, FULL_SCHEMA)
    result, execute, logs = await _sync(reader, {"members": ["A1", "B2"]})
    assert result is None
    execute.assert_not_awaited()
    refusal = next(e for e in logs if e["event"] == "graph_sync_refused_row_type")
    assert refusal["defects"][0]["reason"] == "not a string-keyed map"


# ── T06 [F22] a map member missing a required property is not complete ──────


def test_t06_validation_refuses_a_map_member_missing_its_required_property() -> None:
    result = _validated({"members": [{"line_id": "L1", "direction": "in"}]}, FULL_SCHEMA)
    assert "members" not in result
    # One incomplete member refuses the field: a partial list is not a fact.
    mixed = _validated({"members": [{"code": "A1"}, {"line_id": "L2"}]}, FULL_SCHEMA)
    assert "members" not in mixed


def test_t06_required_property_is_enforced_in_nested_maps() -> None:
    nested = {
        "type": "list",
        "items": {
            "type": "map",
            "properties": [
                {
                    "name": "origin",
                    "type": "map",
                    "properties": [{"name": "code", "type": "string", "required": True}],
                }
            ],
        },
    }
    assert "parts" not in _validated({"parts": [{"origin": {"note": "x"}}]}, {"parts": nested})
    kept = _validated({"parts": [{"origin": {"code": "C"}}]}, {"parts": nested})
    assert kept["parts"] == [{"origin": {"code": "C"}}]


@pytest.mark.asyncio
async def test_t06_t18_incomplete_member_is_never_acknowledged_as_synced(
    reader: DomainYamlReader,
) -> None:
    result, execute, logs = await _sync(reader, {"members": [{"line_id": "L1"}]})
    assert result is None
    execute.assert_not_awaited()
    assert graph_sync_acknowledged(result) is False
    defect = next(e for e in logs if e["event"] == "graph_sync_refused_row_type")["defects"][0]
    assert (defect["field"], defect["reason"]) == ("members[0].code", "required")


# ── T07 [F22] a declared string id is encoded, never guessed ─────────────────


def test_t07_int_line_id_is_encoded_as_its_declared_string() -> None:
    result = _validated({"members": [{"code": "A1", "line_id": 411}]}, FULL_SCHEMA)
    assert result["members"] == [{"code": "A1", "line_id": "411"}]


def test_t07_raw_int_line_id_is_refused_on_the_row(reader: DomainYamlReader) -> None:
    projection = project_graph_sync_row(
        entity_id="widget:1",
        fields={"members": [{"code": "A1", "line_id": 411}]},
        contract=_contract(reader),
    )
    assert projection.row is None
    assert projection.defects[0].field == "members[0].line_id"


# ── T08 [F04] no invalid numeric is coerced into a typed field ───────────────


@pytest.mark.parametrize(
    "value", [True, False, "NaN", "nan", "inf", "-Infinity", math.nan, math.inf, "1e400"]
)
def test_t08_bool_and_non_finite_values_are_not_admitted_as_float(value: Any) -> None:
    assert "weight" not in _validated({"weight": value}, FULL_SCHEMA)


@pytest.mark.parametrize("value", [True, False, 3.7, math.nan, math.inf])
def test_t08_bool_fractional_and_non_finite_values_are_not_admitted_as_int(value: Any) -> None:
    assert "units" not in _validated({"units": value}, FULL_SCHEMA)


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [("weight", "0.94", 0.94), ("weight", 2, 2.0), ("units", "12", 12), ("units", 12.0, 12)],
)
def test_t08_valid_numbers_still_coerce(field: str, value: Any, expected: Any) -> None:
    result = _validated({field: value}, FULL_SCHEMA)
    assert result[field] == expected
    assert type(result[field]) is type(expected)


def test_t08_a_bool_inside_a_declared_map_is_not_a_number() -> None:
    decl = {
        "type": "list",
        "items": {
            "type": "map",
            "properties": [{"name": "qty", "type": "float", "required": True}],
        },
    }
    assert "lots" not in _validated({"lots": [{"qty": True}]}, {"lots": decl})


# ── T09/T10 [F05] the schema-free pass keeps a list's members' shape ─────────


def test_t09_schema_free_list_of_maps_stays_a_list_of_maps() -> None:
    members = [{"code": "A1", "line_id": "L1"}, {"code": "B2"}, {"code": "A1", "line_id": "L1"}]
    result = validate_response({"fields": {"members": members}}, None)
    assert result["members"] == [{"code": "A1", "line_id": "L1"}, {"code": "B2"}]
    assert not any(isinstance(member, str) for member in result["members"])


def test_t09_schema_free_map_members_are_bounded() -> None:
    long = "x" * 1000
    wide = {f"k{i}": i for i in range(80)}
    result = validate_response(
        {"fields": {"members": [{"code": f"  {long}  "}, wide, {1: "non-string key", "ok": "v"}]}},
        None,
    )
    first, second, third = result["members"]
    assert first == {"code": "x" * 256}
    assert list(second) == [f"k{i}" for i in range(50)]
    assert third == {"ok": "v"}
    many = validate_response({"fields": {"members": [{"i": i} for i in range(80)]}}, None)
    assert len(many["members"]) == 50


def test_t09_schema_free_list_of_maps_projects_onto_the_declared_row(
    reader: DomainYamlReader,
) -> None:
    result = validate_response(
        {"fields": {"members": [{"code": "A1", "line_id": "L1", "extra": 1}]}}, None
    )
    projection = project_graph_sync_row(
        entity_id="widget:1", fields={"members": result["members"]}, contract=_contract(reader)
    )
    assert projection.defects == ()
    assert projection.row == {"widget_id": "widget:1", "members": [{"code": "A1", "line_id": "L1"}]}


def test_t10_schema_free_list_of_strings_is_unchanged() -> None:
    result = validate_response(
        {"fields": {"tags": [" a ", "a", "b", "", None, "c" * 300], "label": "  W  "}}, None
    )
    assert result["tags"] == ["a", "b", "c" * 256]
    assert result["label"] == "W"
    many = validate_response({"fields": {"tags": [f"t{i}" for i in range(80)]}}, None)
    assert many["tags"] == [f"t{i}" for i in range(50)]


# ── T11 [F04] a provider field the target did not request is not admitted ───


def test_t11_unrequested_provider_field_is_neither_target_nor_row_data(
    reader: DomainYamlReader,
) -> None:
    validated = _validated({"weight": 1.5, "label": "extra", "rogue": "x"}, {"weight": "float"})
    assert set(validated) == {"confidence", "weight"}
    result: dict[str, Any] = {"fields": {"weight": 1.5, "rogue": "x"}}
    assert restrict_fields_to_schema(result, {"weight": "float"}) == ["rogue"]
    projection = project_graph_sync_row(
        entity_id="widget:1", fields={"weight": 1.5, "rogue": "x"}, contract=_contract(reader)
    )
    assert projection.row == {"widget_id": "widget:1", "weight": 1.5}
    assert projection.dropped == ("rogue",)


# ── T12/T13 [F04/F11] observations are evidence, never admitted ──────────────


IDENTITY = {"subject_ref": "widget:1", "subject_type": None, "role": None}


def _obs(value: Any, ref: str) -> dict[str, Any]:
    return {"field": "members", "value": value, "source_kind": "transaction", "source_ref": ref}


def test_t12_conflicting_list_of_map_observations_keep_the_conflict_policy() -> None:
    evidence = observation_evidence(
        [_obs([{"code": "A1"}], "o:1"), _obs([{"code": "B2"}], "o:2")], IDENTITY
    )
    entry = evidence["members"]
    assert entry["state"] == "conflicting" and entry["admitted"] is False
    assert "value" not in entry and entry["conflict"]["resolution"] is None
    assert [c["value"] for c in entry["conflict"]["candidates"]] == [
        [{"code": "A1"}],
        [{"code": "B2"}],
    ]


def test_t13_observation_only_field_is_evidence_and_never_projected() -> None:
    evidence = observation_evidence([_obs([{"code": "A1"}], "o:1")], IDENTITY)
    assert evidence["members"]["state"] == "observed"
    assert evidence["members"]["admitted"] is False
    assert admitted_fields({"field_evidence": evidence, "fields": {}}) == {}


# ── T14 [F11] unknown or unadmitted evidence is not a graph fact ─────────────


def test_t14_only_admitted_evidence_reaches_the_graph_fields() -> None:
    result = {
        "fields": {"weight": 1.0, "members": [{"code": "A1"}]},
        "field_evidence": {
            "weight": {"state": "unknown", "admitted": False, "value": 1.0},
            "members": {"state": "converged", "admitted": True, "value": [{"code": "A1"}]},
            "label": {"state": "partial", "value": "x"},
        },
    }
    assert admitted_fields(result) == {"members": [{"code": "A1"}]}


# ── T15 [F21] an evidence-only field stays in evidence, off the row ──────────


def test_t15_evidence_only_field_is_retained_in_evidence_and_omitted_from_row(
    reader: DomainYamlReader,
) -> None:
    result = {
        "field_evidence": {
            "grade": {"state": "converged", "admitted": True, "value": "A", "confidence": 0.9}
        }
    }
    fields = admitted_fields(result)
    assert fields == {"grade": "A"}
    projection = project_graph_sync_row(
        entity_id="widget:1", fields=fields, contract=_contract(reader)
    )
    assert projection.row == {"widget_id": "widget:1"}
    assert projection.dropped == ("grade",)


# ── T16 [F04] a request for another cartridge is refused before any work ─────


@pytest.mark.parametrize(
    ("tenant", "cartridge", "code"),
    [
        ("other-tenant", {"id": TENANT, "version": "1.0.0"}, "tenant_cartridge_mismatch"),
        (TENANT, {"id": TENANT, "version": "9.9.9"}, "cartridge_pin_mismatch"),
        (TENANT, {"id": TENANT, "version": "1.0.0", "sha256": "0" * 64}, "cartridge_pin_mismatch"),
    ],
)
def test_t16_wrong_tenant_version_or_hash_is_refused(
    reader: DomainYamlReader, tenant: str, cartridge: dict[str, Any], code: str
) -> None:
    with pytest.raises(ConvergeRequestRefusedError) as refused:
        resolve_converge_request_v2(tenant, _body(cartridge=cartridge), reader)
    assert refused.value.code == code


# ── T17 [F04] a null result never clears a stored graph property ─────────────


def test_t17_null_result_is_omitted_from_the_sync_fields(reader: DomainYamlReader) -> None:
    result = {
        "field_evidence": {
            "weight": {"state": "unknown", "admitted": True, "value": None},
            "label": {"state": "converged", "admitted": True, "value": "W"},
        }
    }
    fields = admitted_fields(result)
    assert fields == {"label": "W"}
    projection = project_graph_sync_row(
        entity_id="widget:1", fields=fields, contract=_contract(reader)
    )
    assert projection.row == {"widget_id": "widget:1", "label": "W"}
    assert admitted_fields({"fields": {"weight": None, "label": "W"}}) == {"label": "W"}


# ── T18 [F22] a completed answer does not make an invalid row synced ─────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"members": '[{"code": "A1"}]'},
        {"weight": math.nan},
        {"weight": True},
        {"members": [{"code": "A1", "line_id": 411}]},
    ],
)
async def test_t18_invalid_row_is_unacknowledged_and_nothing_is_sent(
    reader: DomainYamlReader, fields: dict[str, Any]
) -> None:
    result, execute, logs = await _sync(reader, fields)
    assert result is None
    assert graph_sync_acknowledged(result) is False
    execute.assert_not_awaited()
    assert any(e["event"] == "graph_sync_refused_row_type" for e in logs)
