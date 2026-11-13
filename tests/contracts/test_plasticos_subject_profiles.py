"""PlasticOS material and facility profiles, with explicit EIE/CEG ownership (lane A2s).

The cartridge declares its own subject vocabulary for converge request v2. These
tests pin what that declaration promises: every profile field is a declared
property, every Facility property belongs to exactly one profile, and the
properties CEG alone writes are named, are really CEG's, and are never
declared by EIE. The CEG side is the checked-in fixture
tests/fixtures/ceg/plasticos.spec.yaml, a copy; it cannot detect upstream drift.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.engines.domain_yaml_reader import default_node_label, validate_native_spec
from app.engines.spec_types import row_properties

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
EIE: dict[str, Any] = yaml.safe_load((REPO / "domains/plasticos/spec.yaml").read_bytes())
CEG: dict[str, Any] = yaml.safe_load((REPO / "tests/fixtures/ceg/plasticos.spec.yaml").read_bytes())
TYPES = {entry["name"]: entry for entry in EIE["subjects"]["types"]}
# Added in 0.5.0. All must stay off the CEG sync row.
NEW_EVIDENCE = {"street", "street2", "website", "email", "phone", "material_forms"}


def _props(spec: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    node = next(n for n in spec["ontology"]["nodes"] if n["label"] == label)
    return {prop["name"]: prop for prop in node["properties"]}


def test_vocabulary_is_material_and_facility_for_supplier_and_buyer() -> None:
    assert set(TYPES) == {"facility", "material"}
    assert EIE["subjects"]["roles"] == ["supplier", "buyer"]
    assert validate_native_spec(EIE, "plasticos") == EIE


def test_every_profile_field_is_declared_and_no_property_is_orphaned_or_shared() -> None:
    declared = set(_props(EIE, "Facility")) - {"facility_id"}
    facility, material = (set(TYPES[name]["fields"]) for name in ("facility", "material"))
    assert facility | material == declared
    assert facility & material == set()


def test_ceg_owned_properties_are_cegs_and_never_declared_by_eie() -> None:
    eie, ceg = set(_props(EIE, "Facility")), set(_props(CEG, "Facility"))
    owned = [name for entry in TYPES.values() for name in entry["ceg_owned"]]
    assert len(owned) == len(set(owned))
    # Exactly the properties CEG's Facility declares and EIE's does not.
    assert set(owned) == ceg - eie


def test_new_evidence_never_reaches_a_sync_row() -> None:
    props = _props(EIE, "Facility")
    assert all(props[name].get("evidenceonly") is True for name in NEW_EVIDENCE)
    row = row_properties(EIE, "Facility")
    assert row is not None
    assert NEW_EVIDENCE.isdisjoint(row)
    # The row contract is still a subset of what CEG's Facility declares.
    assert set(row) <= set(_props(CEG, "Facility"))


def test_a_request_that_names_no_node_still_has_one() -> None:
    """Both profiles bind to Facility so v1 requests keep their implicit node."""
    from app.engines.domain_yaml_reader import DomainYamlReader

    config = DomainYamlReader(REPO / "domains").load("plasticos")
    assert default_node_label(config) == "Facility"
    assert {entry["node"] for entry in TYPES.values()} == {"Facility"}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda s: s["subjects"]["types"][0].update(node="Warehouse"), "undeclared node"),
        (lambda s: s["subjects"]["types"][0]["fields"].append("name"), "Duplicate subject field"),
        (lambda s: s["subjects"]["types"][0]["ceg_owned"].append("tier"), "is declared by"),
        (lambda s: s["subjects"]["types"].append(dict(s["subjects"]["types"][0])), "Duplicate"),
        (lambda s: s["subjects"]["roles"].append("supplier"), "Duplicate"),
        (lambda s: s["subjects"].update(matching={}), "Unsupported subjects keys"),
        (lambda s: s["subjects"]["types"][0].update(rank=1), "Unsupported subject type keys"),
        (lambda s: s["subjects"].update(types=[]), "nonempty"),
    ],
)
def test_malformed_subject_vocabulary_is_refused(mutate: Any, message: str) -> None:
    spec = copy.deepcopy(EIE)
    mutate(spec)
    with pytest.raises(ValueError, match=message):
        validate_native_spec(spec, "plasticos")
