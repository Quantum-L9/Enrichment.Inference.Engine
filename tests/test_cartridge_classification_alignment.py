"""Classification and convergence use the cartridge that was actually loaded (A2c).

Before this change a native cartridge (`properties` as a list of declarations,
identity as top-level `name`) classified to zero fields under the domain
`unknown`, and every one-node native cartridge shared one cache entry.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.engines.convergence_controller import _classification_cache, get_or_classify_domain
from app.engines.field_classifier import (
    FieldDifficulty,
    auto_classify_domain,
    classify,
    extract_field_meta,
    spec_domain_name,
)

pytestmark = pytest.mark.unit

PLASTICOS: dict[str, Any] = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "domains/plasticos/spec.yaml").read_bytes()
)
# A second native cartridge with the same node count and no shared field.
CARRIERS: dict[str, Any] = {
    "name": "freight-carriers",
    "version": "0.1.0",
    "ontology": {
        "nodes": [
            {
                "label": "Carrier",
                "properties": [
                    {"name": "legal_name", "type": "string"},
                    {"name": "power_units", "type": "int"},
                    {"name": "hazmat_endorsed", "type": "bool"},
                    {"name": "fleet_class", "type": "string"},
                    "operating_authority",
                    {"type": "string"},
                ],
            }
        ]
    },
}


def _declared(spec: dict[str, Any]) -> set[str]:
    return {
        prop["name"] if isinstance(prop, dict) else prop
        for node in spec["ontology"]["nodes"]
        for prop in node["properties"]
        if isinstance(prop, str) or "name" in prop
    }


@pytest.fixture(autouse=True)
def _empty_cache() -> None:
    _classification_cache.clear()


def test_native_cartridge_properties_are_parsed() -> None:
    fields = extract_field_meta(PLASTICOS)
    assert {meta.name for meta in fields} == _declared(PLASTICOS)
    types = {meta.name: meta.field_type for meta in fields}
    assert types["contamination_tolerance"] == "float"
    assert types["processed_polymers"] == "list"


def test_bare_string_property_is_a_string_field_and_a_nameless_entry_is_skipped() -> None:
    fields = {meta.name: meta.field_type for meta in extract_field_meta(CARRIERS)}
    assert fields["operating_authority"] == "string"
    assert set(fields) == _declared(CARRIERS)


def test_classification_names_the_loaded_cartridge_and_covers_its_fields() -> None:
    for spec in (PLASTICOS, CARRIERS):
        result = auto_classify_domain(spec)
        assert result.domain == spec["name"]
        assert set(result.field_map) == _declared(spec)
        assert sum(result.stats.values()) == len(_declared(spec))
    # Generic rules, applied to whatever the cartridge declares.
    assert auto_classify_domain(CARRIERS).field_map["hazmat_endorsed"] is FieldDifficulty.FINDABLE
    assert auto_classify_domain(CARRIERS).field_map["fleet_class"] is FieldDifficulty.INFERRABLE
    assert auto_classify_domain(PLASTICOS).field_map["tier"] is FieldDifficulty.INFERRABLE


def test_declared_identity_resolution() -> None:
    assert spec_domain_name({"name": "plasticos"}) == "plasticos"
    assert spec_domain_name({"domain": "legacy"}) == "legacy"
    assert spec_domain_name({"metadata": {"domain": "older"}}) == "older"
    assert spec_domain_name({"ontology": {}}) == "unknown"


def test_two_cartridges_never_share_a_classification() -> None:
    plasticos = get_or_classify_domain(PLASTICOS)
    carriers = get_or_classify_domain(CARRIERS)
    assert plasticos.domain == "plasticos"
    assert carriers.domain == "freight-carriers"
    assert set(plasticos.field_map).isdisjoint(carriers.field_map)
    assert get_or_classify_domain(PLASTICOS) is plasticos


def test_a_new_revision_of_one_cartridge_is_reclassified() -> None:
    before = get_or_classify_domain(PLASTICOS)
    revised = copy.deepcopy(PLASTICOS)
    revised["version"] = "0.5.0"
    revised["ontology"]["nodes"][0]["properties"].append(
        {"name": "annual_revenue", "type": "float"}
    )

    after = get_or_classify_domain(revised)

    assert after is not before
    assert "annual_revenue" in after.field_map
    assert "annual_revenue" not in before.field_map


def test_classifying_with_info_logging_enabled_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The module logged structured keywords through a stdlib logger: a TypeError at INFO."""
    with caplog.at_level(logging.DEBUG):
        assert classify(PLASTICOS)
        bad = {
            "name": "x",
            "ontology": {"nodes": [{"properties": [{"name": "f", "difficulty": "nope"}]}]},
        }
        assert classify(bad) == {"f": FieldDifficulty.FINDABLE}
