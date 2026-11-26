"""EIE-D-003: a declared scalar type is enforced at provider admission.

L9_META:
  tier: 2
  domain: typed-evidence
  authority: docs/contracts/converge/request.v2.md section 3.4;
    app/engines/spec_types.py (the one typed-declaration grammar)

A v2 target that omits `type` inherits the cartridge declaration (request.v2
section 3.4). Before this fix the projection kept a typed declaration only for
`list` / `map`, so a scalar `enum` arrived at provider validation as the bare
name `"enum"`, its `values` gone; and `validation_engine.TYPE_MAP` had no entry
for `enum`, `date`, `datetime` or `duration`, so any value at all -- a string
outside the enum, a number, a mapping -- was admitted as-is. The same pass-through
applied inside declared maps and lists.

Owners exercised, earliest first:

* ``converge_request_v2._targets`` -- what a target resolves to;
* ``validation_engine.validate_response`` -- a provider reply to typed fields.

The expected verdict for every value is `spec_types.value_defects`, the grammar
the graph row already enforces, so admission and the row cannot disagree.
PlasticOS cases read the real `domains/plasticos/spec.yaml` declarations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.engines.converge_request_v2 import _targets
from app.engines.spec_types import node_properties, value_defects
from app.services.validation_engine import validate_response

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
GRADE = {"name": "grade", "type": "enum", "values": ["A", "B"], "required": True}


def _plasticos_facility() -> dict[str, dict[str, Any]]:
    spec = yaml.safe_load((REPO / "domains/plasticos/spec.yaml").read_text(encoding="utf-8"))
    properties = node_properties(spec, "Facility")
    assert properties, "the PlasticOS spec declares Facility"
    return {p["name"]: p for p in properties}


def _admitted(field: str, value: Any, declarations: dict[str, dict[str, Any]]) -> dict[str, Any]:
    schema, _ = _targets([{"field": field}], declarations, "Node")
    return validate_response({"fields": {field: value}, "confidence": 0.95}, schema)


# ── The projection keeps what makes a scalar declaration consequential ─────


def test_a_scalar_enum_target_keeps_its_declared_values() -> None:
    schema, _ = _targets([{"field": "grade"}], {"grade": GRADE}, "Node")
    declared = schema["grade"]
    assert isinstance(declared, dict), "an enum target must not collapse to the name 'enum'"
    assert declared["type"] == "enum"
    assert declared["values"] == ["A", "B"]


def test_a_plain_scalar_target_keeps_the_v1_type_name_form() -> None:
    """Unchanged where nothing is lost: string, int, float, bool stay names."""
    declared = {
        "s": {"name": "s", "type": "string"},
        "n": {"name": "n", "type": "int"},
        "f": {"name": "f", "type": "float"},
        "b": {"name": "b", "type": "bool"},
    }
    schema, _ = _targets([{"field": k} for k in declared], declared, "Node")
    assert schema == {"s": "string", "n": "int", "f": "float", "b": "bool"}


# ── Top-level scalars ───────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["C", "a", 123, 1.5, True, {"arbitrary": True}, ["A"]])
def test_a_value_outside_the_enum_is_not_admitted(value: Any) -> None:
    assert "grade" not in _admitted("grade", value, {"grade": GRADE})


@pytest.mark.parametrize("value", ["A", "B"])
def test_a_declared_enum_value_is_admitted(value: Any) -> None:
    assert _admitted("grade", value, {"grade": GRADE})["grade"] == value


@pytest.mark.parametrize(
    ("type_name", "value"),
    [
        ("date", "2026-13-40"),
        ("date", "yesterday"),
        ("date", {"not": "date"}),
        ("date", 20261126),
        ("datetime", "not a time"),
        ("datetime", {"not": "datetime"}),
        ("datetime", 1795700000),
        ("duration", "3 days"),
        ("duration", "P"),
        ("duration", {"not": "duration"}),
        ("duration", 30),
    ],
)
def test_an_invalid_temporal_value_is_not_admitted(type_name: str, value: Any) -> None:
    declared = {"when": {"name": "when", "type": type_name}}
    assert "when" not in _admitted("when", value, declared)


@pytest.mark.parametrize(
    ("type_name", "value"),
    [
        ("date", "2026-11-26"),
        ("datetime", "2026-11-26T17:00:00Z"),
        ("datetime", "2026-11-26T17:00:00+00:00"),
        ("duration", "P3D"),
        ("duration", "PT1H30M"),
    ],
)
def test_a_valid_temporal_value_is_admitted_unchanged(type_name: str, value: Any) -> None:
    declared = {"when": {"name": "when", "type": type_name}}
    assert _admitted("when", value, declared)["when"] == value


# ── Inside declared containers ──────────────────────────────────────────────


def test_an_enum_property_inside_a_declared_map_is_enforced() -> None:
    declared = {"spec": {"name": "spec", "type": "map", "properties": [GRADE]}}
    assert "spec" not in _admitted("spec", {"grade": "C"}, declared)
    assert _admitted("spec", {"grade": "A"}, declared)["spec"] == {"grade": "A"}


def test_a_temporal_property_inside_a_declared_map_is_enforced() -> None:
    since = {"name": "since", "type": "date"}
    declared = {"spec": {"name": "spec", "type": "map", "properties": [since]}}
    assert "spec" not in _admitted("spec", {"since": "not a date"}, declared)
    assert _admitted("spec", {"since": "2026-11-26"}, declared)["spec"] == {"since": "2026-11-26"}


def test_a_list_of_enum_refuses_a_member_outside_the_enum() -> None:
    """One invalid member refuses the list, as a bad member of a numeric list does."""
    declared = {
        "grades": {
            "name": "grades",
            "type": "list",
            "items": {"type": "enum", "values": ["A", "B"]},
        }
    }
    assert "grades" not in _admitted("grades", ["A", "C"], declared)
    assert _admitted("grades", ["A", "B"], declared)["grades"] == ["A", "B"]


def test_a_list_of_dates_refuses_an_invalid_member() -> None:
    declared = {"days": {"name": "days", "type": "list", "items": {"type": "date"}}}
    assert "days" not in _admitted("days", ["2026-11-26", "soon"], declared)
    assert _admitted("days", ["2026-11-26"], declared)["days"] == ["2026-11-26"]


# ── The real PlasticOS evidence-only fields (no graph row re-checks them) ───


@pytest.mark.parametrize("value", ["INVALID", "Post-Consumer", 3, {"kind": "mixed"}])
def test_plasticos_feedstock_type_refuses_a_value_outside_its_enum(value: Any) -> None:
    facility = _plasticos_facility()
    assert facility["feedstock_type"].get("evidenceonly") is True
    assert "feedstock_type" not in _admitted("feedstock_type", value, facility)


def test_plasticos_feedstock_type_admits_a_declared_value() -> None:
    facility = _plasticos_facility()
    allowed = facility["feedstock_type"]["values"][0]
    assert _admitted("feedstock_type", allowed, facility)["feedstock_type"] == allowed


def test_plasticos_equipment_type_ids_refuses_a_nonexistent_machine() -> None:
    facility = _plasticos_facility()
    assert facility["equipment_type_ids"].get("evidenceonly") is True
    known = facility["equipment_type_ids"]["items"]["values"][0]
    value = [known, "nonexistent_machine"]
    assert "equipment_type_ids" not in _admitted("equipment_type_ids", value, facility)
    assert _admitted("equipment_type_ids", [known], facility)["equipment_type_ids"] == [known]


# ── Admission agrees with the grammar the graph row enforces ────────────────


_AGREEMENT_CASES = [
    (GRADE, "A"),
    (GRADE, "C"),
    (GRADE, 7),
    ({"name": "d", "type": "date"}, "2026-11-26"),
    ({"name": "d", "type": "date"}, "nope"),
    ({"name": "t", "type": "datetime"}, "2026-11-26T17:00:00Z"),
    ({"name": "t", "type": "datetime"}, "nope"),
    ({"name": "p", "type": "duration"}, "P1D"),
    ({"name": "p", "type": "duration"}, "one day"),
    ({"name": "g", "type": "list", "items": {"type": "enum", "values": ["A"]}}, ["A"]),
    ({"name": "g", "type": "list", "items": {"type": "enum", "values": ["A"]}}, ["Z"]),
]


@pytest.mark.parametrize(("decl", "value"), _AGREEMENT_CASES)
def test_admission_and_the_row_grammar_reach_the_same_verdict(
    decl: dict[str, Any], value: Any
) -> None:
    conforms = not value_defects(value, decl, decl["name"])
    admitted = decl["name"] in _admitted(decl["name"], value, {decl["name"]: decl})
    assert admitted is conforms, f"admission={admitted}, row grammar conforms={conforms}"


# ── Whitespace normalization is kept for checked list members ───────────────


@pytest.mark.parametrize(
    ("items", "value", "expected"),
    [
        ({"type": "enum", "values": ["A", "B"]}, "A, B", ["A", "B"]),
        ({"type": "enum", "values": ["A", "B"]}, [" A", "B "], ["A", "B"]),
        ({"type": "date"}, "2026-11-26, 2026-11-27", ["2026-11-26", "2026-11-27"]),
        ({"type": "date"}, [" 2026-11-26 "], ["2026-11-26"]),
    ],
    ids=["enum-csv", "enum-padded", "date-csv", "date-padded"],
)
def test_checked_list_members_are_trimmed_before_their_check(
    items: dict[str, Any], value: Any, expected: list[str]
) -> None:
    """Unchecked string members were always trimmed; a checked member is too."""
    declared = {"xs": {"name": "xs", "type": "list", "items": items}}
    assert _admitted("xs", value, declared)["xs"] == expected


def test_a_declared_enum_member_with_padding_is_matched_exactly_first() -> None:
    """Trimming normalizes provider text; it must not reject a declared member as written."""
    padded = {"name": "code", "type": "enum", "values": [" A", "B"]}
    assert _admitted("code", " A", {"code": padded})["code"] == " A"
    assert _admitted("code", " B ", {"code": padded})["code"] == "B"
