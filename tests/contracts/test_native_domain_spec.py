"""Executable EIE native spec contract; PlasticOS is an immutable specimen.

L9_META:
  tier: 2
  domain: enrichment
  authority: native-domain-supply-chain-g27r-v3
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.engines.domain_yaml_reader import validate_native_spec

pytestmark = pytest.mark.unit
SPEC = Path(__file__).resolve().parents[2] / "domains/plasticos/spec.yaml"


@pytest.fixture
def native() -> dict[str, Any]:
    return yaml.safe_load(SPEC.read_bytes())


def test_real_native_spec_preserves_distinct_eie_shape(native: dict[str, Any]) -> None:
    before = deepcopy(native)
    validated = validate_native_spec(native, "plasticos")
    assert validated == before
    assert native == before
    validated["ontology"]["nodes"][0]["properties"].clear()
    assert native == before
    assert "domain" not in native


@pytest.mark.parametrize("raw", [None, [], "plasticos", 4])
def test_native_requires_mapping(raw: object) -> None:
    with pytest.raises(ValueError):
        validate_native_spec(raw, "plasticos")


@pytest.mark.parametrize("tenant", ["Plasticos", "../plasticos", "systemdb", "ab"])
def test_native_requires_legal_tenant(native: dict[str, Any], tenant: str) -> None:
    with pytest.raises(ValueError):
        validate_native_spec(native, tenant)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("name",), "other"),
        (("version",), True),
        (("version",), 0.3),
        (("version",), "draft"),
        (("ontology", "nodes"), {}),
        (("ontology", "nodes"), []),
        (("ontology", "nodes", 0, "label"), "bad label"),
        (("ontology", "nodes", 0, "properties"), {}),
        (("ontology", "nodes", 0, "properties", 0, "name"), ""),
        (("ontology", "nodes", 0, "properties", 0, "type"), "unsupported"),
        (("ontology", "nodes", 0, "properties", 0, "required"), "yes"),
        (("ontology", "nodes", 0, "enrichment_hints"), "bad"),
        (("ontology", "nodes", 0, "enrichment_hints", "priority_fields"), ["missing"]),
        (("ontology", "nodes", 0, "enrichment_hints", "min_confidence"), True),
        (("ontology", "nodes", 0, "enrichment_hints", "min_confidence"), 1.1),
        (("ontology", "nodes", 0, "enrichment_hints", "refresh_interval_days"), -1),
        (("ontology", "nodes", 0, "enrichment_hints", "max_variations_discovery"), 0),
        (("ontology", "nodes", 0, "enrichment_hints", "objective_template"), ""),
        (("inference_rules",), {}),
        (("inference_rules", 0, "conditions"), []),
        (("inference_rules", 0, "conditions", 0, "operator"), "EXEC"),
        (("inference_rules", 0, "conditions", 0, "field"), "missing"),
        (("inference_rules", 0, "outputs", 0, "derivation_type"), "ranking"),
        (("inference_rules", 0, "outputs", 0, "field"), "missing"),
        (("inference_rules", 0, "confidence"), -0.1),
        (("inference_rules", 0, "rule_or_model_version"), ""),
        (("semantic_registry", "enabled"), "true"),
        (("semantic_registry", "canonical_entities"), {}),
        (("semantic_registry", "canonical_entities", 0, "entity_type"), "Unknown"),
        (("semantic_registry", "canonical_entities", 0, "resolution_strategy"), "rank"),
        (("semantic_registry", "canonical_entities", 0, "resolution_threshold"), 1.5),
        (("semantic_registry", "canonical_entities", 0, "key_fields"), ["missing"]),
        (("sync", "endpoints"), {}),
        (("sync", "endpoints", 0, "path"), "https://elsewhere/v1/sync/facilities"),
        (("sync", "endpoints", 0, "path"), "/v1/sync/../facilities"),
        (("sync", "endpoints", 0, "targetnode"), "bad label"),
        (("sync", "endpoints", 0, "idproperty"), ""),
        (("sync", "endpoints", 0, "batchstrategy"), "DELETE"),
        (("gates",), [{"name": "ranking"}]),
        (("scoring", "dimensions"), [{"weight": 1}]),
        (("ranking",), {"weights": [1]}),
        (("matchentities",), [{"label": "Facility"}]),
        (("match_entities",), ["Facility"]),
    ],
)
def test_hostile_native_variant_is_rejected(
    native: dict[str, Any], path: tuple[str | int, ...], value: object
) -> None:
    target = native
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        validate_native_spec(native, "plasticos")


@pytest.mark.parametrize("section", ["nodes", "properties", "rules", "endpoints", "entities"])
def test_duplicate_declarations_fail(native: dict[str, Any], section: str) -> None:
    declarations = {
        "nodes": native["ontology"]["nodes"],
        "properties": native["ontology"]["nodes"][0]["properties"],
        "rules": native["inference_rules"],
        "endpoints": native["sync"]["endpoints"],
        "entities": native["semantic_registry"]["canonical_entities"],
    }[section]
    declarations.append(deepcopy(declarations[0]))
    with pytest.raises(ValueError):
        validate_native_spec(native, "plasticos")


@pytest.mark.parametrize(
    "path",
    ["../rules.yaml", "/tmp/rules.yaml", "https://x/rules", "a/../rules.yaml", "a\\rules.yaml"],
)
def test_rule_references_are_bounded(native: dict[str, Any], path: str) -> None:
    native.pop("inference_rules")
    native["inference_rules_path"] = path
    with pytest.raises(ValueError):
        validate_native_spec(native, "plasticos")


def test_safe_draft_reference_and_semver_are_preserved(native: dict[str, Any]) -> None:
    native.pop("inference_rules")
    native["inference_rules_path"] = "rules/inference.yaml"
    native["version"] = "0.4.0-discovered"
    assert validate_native_spec(native, "plasticos") == native


def test_conflicting_legacy_identity_is_not_native_authority(native: dict[str, Any]) -> None:
    native["domain"] = {"id": "other", "version": "99.0.0"}
    with pytest.raises(ValueError):
        validate_native_spec(native, "plasticos")


@pytest.mark.parametrize(
    ("key", "value"),
    [("type", []), ("type", {}), ("type", None), ("name", "bad-name")],
)
def test_malformed_property_is_a_validation_error(
    native: dict[str, Any], key: str, value: object
) -> None:
    native["ontology"]["nodes"][0]["properties"][0][key] = value
    with pytest.raises(ValueError):
        validate_native_spec(native, "plasticos")


@pytest.mark.parametrize("version", ["01.0.0", "1.0.0-01", "1.0.0-alpha.02", "1.0", "1.0.0 "])
def test_invalid_semver_is_refused(native: dict[str, Any], version: str) -> None:
    native["version"] = version
    with pytest.raises(ValueError):
        validate_native_spec(native, "plasticos")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "0.9", None])
def test_nonfinite_or_wrong_type_confidence_refused(native: dict[str, Any], value: object) -> None:
    native["inference_rules"][0]["confidence"] = value
    with pytest.raises(ValueError):
        validate_native_spec(native, "plasticos")


def test_unknown_hint_key_refused(native: dict[str, Any]) -> None:
    native["ontology"]["nodes"][0]["enrichment_hints"]["ranking"] = {"weight": 1}
    with pytest.raises(ValueError):
        validate_native_spec(native, "plasticos")
