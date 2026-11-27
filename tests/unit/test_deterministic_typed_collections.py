"""The deterministic provider synthesizes a declared list<map> / map by its declaration.

A converge target for a ``list`` or ``map`` field carries its typed declaration
(request v2 ``_targets``; a v1 ``schema`` may carry the same form). Before this,
every ``list`` was synthesized as ``["det:<digest>"]`` -- a list of strings, never
a valid ``list<map>`` -- so an enriched childsync collection could not reach a
typed sync row in a hermetic run. The shape comes only from the declaration the
provider is handed: no domain, cartridge or field name is known here.

Shapes: the gate_loop synthneutral cartridge's ``handled_kinds`` (a childsync
field) and a second synthetic shape with numeric, boolean and nested-map leaves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.engines.packet_router import project_graph_sync_row, resolve_graph_sync_contract
from app.services.deterministic_provider import (
    DETERMINISTIC_VALUE_PREFIX,
    build_deterministic_payload,
)

pytestmark = pytest.mark.unit

SYNTH_SPEC = (
    Path(__file__).resolve().parents[2] / "tests/e2e/gate_loop/cartridges/synthneutral/spec.yaml"
)
ENTITY = {"id": "st-1", "station_id": "st-1"}

# A second, unrelated shape: every scalar kind plus a nested map.
GAUGES: dict[str, Any] = {
    "type": "list",
    "items": {
        "type": "map",
        "properties": [
            {"name": "tag", "type": "string", "required": True},
            {"name": "reading", "type": "float"},
            {"name": "count", "type": "int"},
            {"name": "active", "type": "bool"},
            {
                "name": "site",
                "type": "map",
                "properties": [{"name": "ref", "type": "string"}, {"name": "level", "type": "int"}],
            },
        ],
    },
}


def _spec() -> dict[str, Any]:
    loaded = yaml.safe_load(SYNTH_SPEC.read_bytes())
    assert isinstance(loaded, dict)
    return loaded


def _declaration(spec: dict[str, Any], node: str, field: str) -> dict[str, Any]:
    """The field's declaration exactly as the spec carries it (the target form)."""
    nodes = spec["ontology"]["nodes"]
    props = next(n for n in nodes if n["label"] == node)["properties"]
    prop = next(p for p in props if p["name"] == field)
    return {k: prop[k] for k in ("type", "items", "properties", "required") if k in prop}


def _marked(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(DETERMINISTIC_VALUE_PREFIX)


def test_a_declared_childsync_list_of_maps_is_synthesized_as_maps() -> None:
    spec = _spec()
    declared = _declaration(spec, "Station", "handled_kinds")
    value = build_deterministic_payload(ENTITY, {"handled_kinds": declared})["fields"][
        "handled_kinds"
    ]
    keys = [p["name"] for p in declared["items"]["properties"]]
    assert isinstance(value, list) and len(value) == 1
    (item,) = value
    assert list(item) == keys
    for prop in declared["items"]["properties"]:
        if prop["type"] == "string":
            assert _marked(item[prop["name"]])  # every string leaf keeps the det: marker
        elif prop["type"] == "enum":
            assert item[prop["name"]] in prop["values"]  # type-correct, like int/float/bool


def test_the_synthesized_collection_projects_onto_a_typed_childsync_row() -> None:
    """What reaches CEG: the endpoint's typed row, the collection as child maps."""
    spec = _spec()
    declared = _declaration(spec, "Station", "handled_kinds")
    fields = build_deterministic_payload(ENTITY, {"handled_kinds": declared})["fields"]
    contract = resolve_graph_sync_contract(spec, node_label="Station")
    assert contract is not None
    projection = project_graph_sync_row(entity_id="st-1", fields=fields, contract=contract)
    assert projection.defects == ()
    assert projection.row is not None
    assert projection.row["handled_kinds"] == fields["handled_kinds"]


def test_every_leaf_follows_its_own_declaration() -> None:
    (item,) = build_deterministic_payload(ENTITY, {"gauges": GAUGES})["fields"]["gauges"]
    assert _marked(item["tag"])
    assert isinstance(item["reading"], float)
    assert isinstance(item["count"], int) and not isinstance(item["count"], bool)
    assert isinstance(item["active"], bool)
    assert set(item["site"]) == {"ref", "level"}
    assert _marked(item["site"]["ref"]) and isinstance(item["site"]["level"], int)


def test_a_map_declaration_is_one_map_and_scalar_items_stay_scalars() -> None:
    fields = build_deterministic_payload(
        ENTITY,
        {
            "site": {"type": "map", "properties": [{"name": "ref", "type": "string"}]},
            "levels": {"type": "list", "items": {"type": "int"}},
        },
    )["fields"]
    assert set(fields["site"]) == {"ref"} and _marked(fields["site"]["ref"])
    assert len(fields["levels"]) == 1 and isinstance(fields["levels"][0], int)


def test_synthesis_is_reproducible_and_entity_specific() -> None:
    schema = {"gauges": GAUGES}
    first = build_deterministic_payload(ENTITY, schema)
    assert build_deterministic_payload(dict(ENTITY), dict(schema)) == first
    other = build_deterministic_payload({"id": "st-2"}, schema)
    assert other["fields"] != first["fields"]
    assert first["synthetic_fields"] == ["gauges"]


def test_a_bare_list_type_names_no_item_shape_and_is_unchanged() -> None:
    (value,) = build_deterministic_payload(ENTITY, {"tags": "list"})["fields"]["tags"]
    assert _marked(value)


def test_a_value_the_entity_carries_is_still_echoed_not_synthesized() -> None:
    given = [{"code": "K9"}]
    payload = build_deterministic_payload(
        {**ENTITY, "handled_kinds": given},
        {"handled_kinds": _declaration(_spec(), "Station", "handled_kinds")},
    )
    assert payload["fields"]["handled_kinds"] == given
    assert payload["synthetic_fields"] == []
