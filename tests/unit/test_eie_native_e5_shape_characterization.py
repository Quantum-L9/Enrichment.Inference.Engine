"""Characterization baseline of the PlasticOS-only shaper (campaign EIE-NATIVE-CLOSURE-001, E5).

``app.engines.polymer_capability`` is a cartridge-named runtime branch on the
converge graph leg (``handlers.py`` call site). The PlasticOS relocation
campaign retires it later and must reproduce exactly this behavior through the
generic spec-driven route. These tests freeze what the module does TODAY. They
assert that the behavior is current, not that it is correct.

L9_META:
  tier: 2
  domain: graph-sync
  authority: EIE-NATIVE-CLOSURE-001 lane L3 (operator decision D6)
"""

from __future__ import annotations

from typing import Any

import pytest

from app.engines.polymer_capability import (
    PLASTICOS_CARTRIDGE,
    prepare_processed_polymers,
    shape_graph_fields,
)

pytestmark = pytest.mark.unit


def _shape(polymers: Any, domain: str = "plasticos", **other: Any) -> dict[str, Any]:
    return shape_graph_fields(domain, {"processed_polymers": polymers, **other})


def test_cartridge_name_is_the_branch_key() -> None:
    assert PLASTICOS_CARTRIDGE == "plasticos"


def test_valid_odoo_line_becomes_one_wire_element() -> None:
    shaped = _shape(
        [{"polymer": "HDPE", "capability_id": [411, "Line 411"], "direction": "in", "form": "Pellet"}]
    )
    assert shaped == {
        "processed_polymers": [
            {"code": "HDPE", "line_id": "411", "direction": "in", "form": "Pellet"}
        ]
    }


@pytest.mark.parametrize(
    ("capability_id", "line_id"),
    [(411, "411"), ("411", "411"), (" 411 ", "411"), ([411, "L"], "411"), ({"id": 411}, "411")],
)
def test_capability_line_shapes_are_copied_as_a_string_line_id(
    capability_id: Any, line_id: str
) -> None:
    shaped = _shape([{"polymer": "HDPE", "capability_id": capability_id}])
    assert shaped["processed_polymers"] == [{"code": "HDPE", "line_id": line_id}]


def test_polymer_id_code_is_used_when_polymer_is_absent() -> None:
    shaped = _shape([{"polymer_id": {"code": " PP "}, "capability_id": 7}])
    assert shaped["processed_polymers"] == [{"code": "PP", "line_id": "7"}]


@pytest.mark.parametrize(
    "profile",
    [
        {"capability_id": 411},  # missing code
        {"polymer": "  ", "capability_id": 411},  # blank code
        {"polymer_id": {"code": ""}, "capability_id": 411},
    ],
)
def test_profile_without_a_polymer_code_is_left_out(profile: dict[str, Any]) -> None:
    assert _shape([profile])["processed_polymers"] == []


@pytest.mark.parametrize("capability_id", [None, False, True, "", "  ", [], {"id": None}])
def test_profile_without_a_capability_line_is_left_out(capability_id: Any) -> None:
    shaped = _shape(
        [{"polymer": "HDPE", "capability_id": capability_id}, {"polymer": "PP", "capability_id": 9}]
    )
    assert shaped["processed_polymers"] == [{"code": "PP", "line_id": "9"}]


def test_direction_and_form_are_copied_only_when_stored() -> None:
    shaped = _shape(
        [
            {"polymer": "HDPE", "capability_id": 1, "direction": "in"},
            {"polymer": "PP", "capability_id": 2, "form": "Flake"},
            {"polymer": "PET", "capability_id": 3, "direction": "", "form": False},
            {"polymer": "LDPE", "capability_id": 4, "direction": "out", "form": "Regrind"},
        ]
    )
    assert shaped["processed_polymers"] == [
        {"code": "HDPE", "line_id": "1", "direction": "in"},
        {"code": "PP", "line_id": "2", "form": "Flake"},
        {"code": "PET", "line_id": "3"},
        {"code": "LDPE", "line_id": "4", "direction": "out", "form": "Regrind"},
    ]


def test_policy_fields_on_the_profile_are_not_copied() -> None:
    shaped = _shape(
        [
            {
                "polymer": "HDPE",
                "capability_id": 5,
                "capacity": 900,
                "contamination": 2,
                "grade": "A",
                "color": "natural",
            }
        ]
    )
    assert shaped["processed_polymers"] == [{"code": "HDPE", "line_id": "5"}]


def test_duplicate_line_is_kept_twice_not_deduplicated() -> None:
    line = {"polymer": "HDPE", "capability_id": 411}
    shaped = _shape([line, dict(line)])
    assert shaped["processed_polymers"] == [
        {"code": "HDPE", "line_id": "411"},
        {"code": "HDPE", "line_id": "411"},
    ]


def test_already_wire_shaped_input_is_returned_unchanged() -> None:
    wire = [{"code": "HDPE", "line_id": "411", "direction": "in"}, {"code": "PP"}]
    assert _shape(wire)["processed_polymers"] is wire
    assert prepare_processed_polymers(wire) is wire


def test_mixed_list_is_converted_once_any_member_has_capability_id() -> None:
    mixed = [{"code": "PP", "line_id": "9"}, {"polymer": "HDPE", "capability_id": 411}]
    # The wire-shaped member has no polymer/capability_id, so it is dropped.
    assert _shape(mixed)["processed_polymers"] == [{"code": "HDPE", "line_id": "411"}]


@pytest.mark.parametrize("value", ["HDPE", '[{"polymer": "HDPE"}]', 7, {"polymer": "HDPE"}])
def test_non_list_value_is_returned_unchanged(value: Any) -> None:
    assert _shape(value)["processed_polymers"] == value


def test_non_mapping_members_are_skipped_after_conversion() -> None:
    shaped = _shape(["HDPE", {"polymer": "PP", "capability_id": 1}])
    assert shaped["processed_polymers"] == [{"code": "PP", "line_id": "1"}]


def test_fields_without_processed_polymers_are_returned_as_is() -> None:
    fields = {"name": "Acme"}
    assert shape_graph_fields("plasticos", fields) is fields


def test_null_processed_polymers_is_returned_as_is() -> None:
    fields = {"processed_polymers": None}
    assert shape_graph_fields("plasticos", fields) is fields


def test_other_fields_are_carried_and_the_input_is_not_mutated() -> None:
    fields = {"processed_polymers": [{"polymer": "HDPE", "capability_id": 1}], "tier": "tier_1"}
    shaped = shape_graph_fields("plasticos", fields)
    assert shaped == {"processed_polymers": [{"code": "HDPE", "line_id": "1"}], "tier": "tier_1"}
    assert fields["processed_polymers"] == [{"polymer": "HDPE", "capability_id": 1}]


@pytest.mark.parametrize("domain", ["", "metals", "PlasticOS", "plasticos-staging"])
def test_non_plasticos_domain_is_passed_through_untouched(domain: str) -> None:
    fields = {"processed_polymers": [{"polymer": "HDPE", "capability_id": 411}]}
    assert shape_graph_fields(domain, fields) is fields
