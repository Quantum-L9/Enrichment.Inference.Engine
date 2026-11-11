"""Typed spec contract: the shared declaration syntax, the typed row, typed coercion.

Owner decision 2026-11-11 — the domain spec is the cartridge: the row schema
each CEG sync endpoint accepts is the tenant spec's declared types. These tests
pin the three EIE halves of that contract:

* the spec reader adopts the shared syntax (``items`` iff list, ``properties``
  iff map, no list of lists) and refuses an untyped ``list``;
* ``notify_graph_sync`` projects every row onto the declaration: a
  ``list<map>`` travels as a list of maps, a mismatched value refuses the row
  before anything is sent, an undeclared field is dropped;
* ``validation_engine._coerce`` keeps a list's map items as maps.

L9_META:
  tier: 2
  domain: graph-sync
  authority: owner decision 2026-11-11 (typed spec contract)
  pr_class: app_code + tier2_test
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import structlog
import yaml
from constellation_node_sdk.transport import create_transport_packet

from app.engines.domain_yaml_reader import DomainYamlReader, validate_native_spec
from app.engines.packet_router import PacketRouter
from app.services.validation_engine import _coerce, validate_response

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "domains/plasticos/spec.yaml"
GATE = "https://gate-node:8080"


def _native() -> dict[str, Any]:
    return yaml.safe_load(SPEC.read_text())


def _facility(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return spec["ontology"]["nodes"][0]["properties"]


# ── 1. The spec reader adopts the shared syntax ──────────────────────────────


def test_reader_refuses_a_list_without_items() -> None:
    spec = _native()
    _facility(spec).append({"name": "aliases", "type": "list"})
    with pytest.raises(ValueError, match="requires `items`"):
        validate_native_spec(spec, "plasticos")


@pytest.mark.parametrize(
    "declaration",
    [
        {"name": "certifications_seen", "type": "list", "items": {"type": "string"}},
        {
            "name": "location",
            "type": "map",
            "properties": [{"name": "lat", "type": "float"}],
            "evidenceonly": True,
        },
        {
            "name": "sites",
            "type": "list",
            "items": {
                "type": "map",
                "properties": [{"name": "code", "type": "string", "required": True}],
            },
            "evidenceonly": True,
        },
        {"name": "grade", "type": "enum", "values": ["A", "B"]},
    ],
)
def test_reader_accepts_the_shared_syntax(declaration: dict[str, Any]) -> None:
    spec = _native()
    _facility(spec).append(declaration)
    assert validate_native_spec(spec, "plasticos") == spec


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        ({"name": "x", "type": "map"}, "requires nonempty `properties`"),
        ({"name": "x", "type": "map", "properties": []}, "requires nonempty `properties`"),
        (
            {"name": "x", "type": "list", "items": {"type": "list", "items": {"type": "string"}}},
            "list of lists",
        ),
        ({"name": "x", "type": "string", "items": {"type": "string"}}, "only declarable"),
        (
            {"name": "x", "type": "list", "items": {"type": "string"}, "properties": []},
            "belongs to a map",
        ),
        ({"name": "x", "type": "dict"}, "not a typed declaration"),
        ({"name": "x", "type": "object"}, "not a typed declaration"),
        ({"name": "x", "type": "list", "items": {"type": "nope"}}, "unsupported type"),
        ({"name": "x", "type": "string", "evidenceonly": "yes"}, "evidenceonly must be boolean"),
    ],
)
def test_reader_refuses_declarations_outside_the_syntax(
    declaration: dict[str, Any], message: str
) -> None:
    spec = _native()
    _facility(spec).append(declaration)
    with pytest.raises(ValueError, match=message):
        validate_native_spec(spec, "plasticos")


def test_a_row_map_shape_must_be_a_childsync_field() -> None:
    """CEG: map and list<map> are wire-only shapes, valid only as a childsync field."""
    spec = _native()
    _facility(spec).append(
        {"name": "location", "type": "map", "properties": [{"name": "lat", "type": "float"}]}
    )
    with pytest.raises(ValueError, match="only as the field of a childsync"):
        validate_native_spec(spec, "plasticos")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda spec: spec["sync"]["endpoints"][0]["childsync"].append(
                {
                    "field": "name",
                    "targetnode": "Alias",
                    "targetid": "code",
                    "edgetype": "KNOWN_AS",
                    "edgedirection": "parenttochild",
                }
            ),
            "childsync field name must be a declared map or list<map>",
        ),
        (
            lambda spec: spec["sync"]["endpoints"][0]["childsync"][0].update({"targetid": "id"}),
            "items must declare id",
        ),
        (
            lambda spec: spec["sync"]["endpoints"][0]["childsync"][0].update(
                {"edgedirection": "sideways"}
            ),
            "edgedirection",
        ),
        (
            lambda spec: spec["sync"]["endpoints"][0].update(
                {
                    "taxonomyedges": [
                        {
                            "field": "processed_polymers",
                            "edgetype": "X",
                            "targetlabel": "PolymerFamily",
                            "targetid": "code",
                        }
                    ]
                }
            ),
            "taxonomy field processed_polymers",
        ),
        (
            lambda spec: spec["sync"]["endpoints"][0].update({"idproperty": "city"}),
            "idproperty city must be a declared scalar row property",
        ),
    ],
)
def test_sync_row_contract_is_validated(mutate: Any, message: str) -> None:
    spec = _native()
    mutate(spec)
    with pytest.raises(ValueError, match=re.escape(message)):
        validate_native_spec(spec, "plasticos")


# ── 2. notify_graph_sync sends exactly the declared, typed row ──────────────


def _ack(entity_type: str = "facilities") -> object:
    return create_transport_packet(
        action="sync",
        payload={"status": "success", "entity_type": entity_type, "synced_count": 1},
        tenant="plasticos",
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )


def _router() -> PacketRouter:
    return PacketRouter(gate_url=GATE, domain_reader=DomainYamlReader(ROOT / "domains"))


async def _sync(fields: dict[str, Any]) -> tuple[dict[str, Any] | None, AsyncMock]:
    router = _router()
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_ack()
    ) as execute:
        result = await router.notify_graph_sync(
            tenant_id="plasticos",
            entity_id="fac-1",
            fields=copy.deepcopy(fields),
            domain="plasticos",
            node_label="Facility",
        )
    return result, execute


@pytest.mark.asyncio
async def test_list_of_maps_is_sent_as_a_list_of_maps() -> None:
    result, execute = await _sync(
        {
            "name": "Acme Recycling",
            "processed_polymers": [{"code": "HDPE"}, {"code": "PET", "note": "undeclared"}],
            "min_density": 0.94,
            "capacity_tons_month": 500,
        }
    )
    assert result is not None
    row = execute.await_args.kwargs["payload"]["batch"][0]
    assert row == {
        "facility_id": "fac-1",
        "name": "Acme Recycling",
        "processed_polymers": [{"code": "HDPE"}, {"code": "PET"}],
        "min_density": 0.94,
        "capacity_tons_month": 500.0,
    }
    assert isinstance(row["processed_polymers"], list)
    assert all(isinstance(item, dict) for item in row["processed_polymers"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "declared", "actual"),
    [
        ("processed_polymers", '[{"code": "HDPE"}]', "list<map{code}>", "string"),
        ("processed_polymers", ["HDPE", "PET"], "map{code}", "string"),
        ("processed_polymers", [{"name": "HDPE"}], "string", "missing"),
        ("min_density", "high", "float", "string"),
        ("capacity_tons_month", True, "float", "bool"),
        ("tier", 5, "string", "int"),
    ],
)
async def test_a_value_that_does_not_match_its_declaration_refuses_the_row(
    field: str, value: Any, declared: str, actual: str
) -> None:
    with structlog.testing.capture_logs() as logs:
        result, execute = await _sync({"name": "Acme", field: value})
    assert result is None
    execute.assert_not_awaited()
    refusal = next(e for e in logs if e["event"] == "graph_sync_refused_row_type")
    defect = refusal["defects"][0]
    assert defect["field"].startswith(field)
    assert (defect["declared"], defect["actual"]) == (declared, actual)


@pytest.mark.asyncio
async def test_undeclared_and_evidence_only_fields_are_dropped_and_counted() -> None:
    with structlog.testing.capture_logs() as logs:
        result, execute = await _sync(
            {
                "name": "Acme",
                "city": "Austin",  # evidenceonly: EIE's, never on a row
                "material_grade": "A",  # evidenceonly
                "density_min": 0.9,  # pre-alignment name: undeclared
                "enriched_by": "x",
            }
        )
    assert result is not None
    assert execute.await_args.kwargs["payload"]["batch"] == [
        {"facility_id": "fac-1", "name": "Acme"}
    ]
    dropped = next(e for e in logs if e["event"] == "graph_sync_undeclared_fields_dropped")
    assert dropped["dropped_count"] == 4
    assert set(dropped["dropped"]) == {"city", "material_grade", "density_min", "enriched_by"}


# ── 3. Coercion keeps declared list item types ───────────────────────────────


def test_coerce_keeps_map_items_as_maps() -> None:
    assert _coerce([{"code": "PET"}, {"code": "HDPE"}], list) == [
        {"code": "PET"},
        {"code": "HDPE"},
    ]


def test_coerce_applies_the_declared_item_type() -> None:
    items = {"type": "map", "properties": [{"name": "code", "type": "string"}]}
    assert _coerce([{"code": 7, "extra": 1}], list, items) == [{"code": "7"}]
    assert _coerce(["1.5", 2], list, {"type": "float"}) == [1.5, 2.0]
    with pytest.raises(TypeError):
        _coerce(["HDPE"], list, items)


def test_untyped_list_of_strings_is_unchanged() -> None:
    assert _coerce([" HDPE ", "PP", "PP", ""], list) == ["HDPE", "PP"]
    assert _coerce("HDPE, PP", list) == ["HDPE", "PP"]


def test_validate_response_honours_a_typed_declaration() -> None:
    schema = {
        "processed_polymers": {
            "type": "list",
            "items": {"type": "map", "properties": [{"name": "code", "type": "string"}]},
        },
        "min_density": "float",
    }
    result = validate_response(
        {"fields": {"processed_polymers": [{"code": "PET"}], "min_density": "0.9"}}, schema
    )
    assert result["processed_polymers"] == [{"code": "PET"}]
    assert result["min_density"] == 0.9


# ── 4. A discovered list field is proposed as a typed declaration ───────────


@pytest.mark.parametrize(
    ("samples", "items"),
    [
        ([["HDPE", "PP"], ["PET"]], {"type": "string"}),
        ([[1, 2.5]], {"type": "float"}),
        (
            [[{"code": "HDPE"}], [{"code": "PET"}]],
            {"type": "map", "properties": [{"name": "code", "type": "string"}]},
        ),
        ([["HDPE", {"code": "PET"}]], {"type": "string"}),
        ([], {"type": "string"}),
    ],
)
def test_a_proposed_list_declares_its_items(samples: list[Any], items: dict[str, Any]) -> None:
    from app.engines.convergence.schema_proposer import FieldProposal, _typed_definition
    from app.engines.spec_types import validate_declaration

    definition = _typed_definition(
        FieldProposal(field_name="polymers_seen", field_type="list", sample_values=samples)
    )
    assert definition == {"type": "list", "source": "enrichment", "items": items}
    validate_declaration(definition, "polymers_seen")


def test_retyping_a_list_drops_its_old_items() -> None:
    from app.engines.convergence.schema_proposer import _set_property

    props = [{"name": "x", "type": "list", "items": {"type": "string"}, "description": "kept"}]
    _set_property(props, "x", {"type": "float", "source": "enrichment"}, allow_overwrite=True)
    assert props == [{"name": "x", "type": "float", "source": "enrichment", "description": "kept"}]
