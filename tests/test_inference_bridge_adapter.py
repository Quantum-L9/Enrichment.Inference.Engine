"""
Tests for InferenceBridge adapter — v1 API surface backed by v2 engine.

Validates:
  1. v1 contract preservation (derived_fields, confidence_map, rules_fired)
  2. v2 feature exposure (unlock_map, blocked_fields)
  3. Fallback behavior when no domain_spec provided
  4. get_rule_catalog() parity between graph-based and flat-rules
"""

import pytest

from app.engines.inference_bridge_adapter import InferenceBridge, InferenceResult

# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture
def plastics_domain_spec():
    """Minimal domain spec with derived_from declarations."""
    return {
        "domain": "plastics_recycling",
        "ontology": {
            "nodes": {
                "Facility": {
                    "properties": {
                        "polymer_type": {
                            "type": "string",
                        },
                        "contamination_pct": {
                            "type": "float",
                        },
                        "certifications": {
                            "type": "list",
                        },
                        "material_grade": {
                            "type": "string",
                            "managed_by": "computed",
                            "derived_from": [
                                "polymer_type",
                                "contamination_pct",
                            ],
                            "inference_rule": "material_grade_lookup",
                            "confidence_floor": 0.6,
                        },
                        "facility_tier": {
                            "type": "string",
                            "managed_by": "inference",
                            "derived_from": [
                                "certifications",
                                "material_grade",
                            ],
                            "inference_rule": "facility_tier_compute",
                            "confidence_floor": 0.7,
                        },
                    },
                },
            },
        },
    }


@pytest.fixture
def flat_rules():
    """v1-style flat rules for fallback testing."""
    return [
        {
            "name": "premium_hdpe",
            "requires": ["polymer_type", "contamination_pct"],
            "produces": {"material_grade": "Premium HDPE"},
            "confidence": 0.9,
        },
    ]


@pytest.fixture
def complete_entity():
    """Entity with all inputs satisfied."""
    return {
        "polymer_type": "HDPE",
        "contamination_pct": 1.5,
        "certifications": ["ISO 9001", "R2"],
    }


@pytest.fixture
def partial_entity():
    """Entity missing certifications."""
    return {
        "polymer_type": "HDPE",
        "contamination_pct": 1.5,
    }


# ── v1 Contract Tests ────────────────────────────────────


class TestV1ContractPreservation:
    """Every v1 consumer expectation must hold."""

    def test_constructor_accepts_rules_only(self, flat_rules):
        bridge = InferenceBridge(rules=flat_rules)
        assert bridge is not None

    def test_constructor_accepts_domain_spec(self, plastics_domain_spec):
        bridge = InferenceBridge(domain_spec=plastics_domain_spec)
        assert bridge.graph is not None

    def test_run_returns_inference_result(self, plastics_domain_spec, partial_entity):
        bridge = InferenceBridge(domain_spec=plastics_domain_spec)
        result = bridge.run(partial_entity, {"polymer_type": 0.9, "contamination_pct": 0.8})
        assert isinstance(result, InferenceResult)

    def test_result_has_derived_fields_dict(self, plastics_domain_spec, partial_entity):
        bridge = InferenceBridge(domain_spec=plastics_domain_spec)
        result = bridge.run(partial_entity, {"polymer_type": 0.9, "contamination_pct": 0.8})
        assert isinstance(result.derived_fields, dict)

    def test_result_has_confidence_map_dict(self, plastics_domain_spec, partial_entity):
        bridge = InferenceBridge(domain_spec=plastics_domain_spec)
        result = bridge.run(partial_entity, {"polymer_type": 0.9, "contamination_pct": 0.8})
        assert isinstance(result.confidence_map, dict)

    def test_result_has_rules_fired_int(self, plastics_domain_spec, partial_entity):
        bridge = InferenceBridge(domain_spec=plastics_domain_spec)
        result = bridge.run(partial_entity, {"polymer_type": 0.9, "contamination_pct": 0.8})
        assert isinstance(result.rules_fired, int)

    def test_result_has_rules_skipped_int(self, plastics_domain_spec, partial_entity):
        bridge = InferenceBridge(domain_spec=plastics_domain_spec)
        result = bridge.run(partial_entity, {"polymer_type": 0.9, "contamination_pct": 0.8})
        assert isinstance(result.rules_skipped, int)

    def test_result_has_rule_trace_list(self, plastics_domain_spec, partial_entity):
        bridge = InferenceBridge(domain_spec=plastics_domain_spec)
        result = bridge.run(partial_entity, {"polymer_type": 0.9, "contamination_pct": 0.8})
        assert isinstance(result.rule_trace, list)

    def test_get_rule_catalog_returns_list_of_dicts(self, plastics_domain_spec):
        bridge = InferenceBridge(domain_spec=plastics_domain_spec)
        catalog = bridge.get_rule_catalog()
        assert isinstance(catalog, list)
        assert all(isinstance(r, dict) for r in catalog)

    def test_catalog_entries_have_name_requires_produces(self, plastics_domain_spec):
        bridge = InferenceBridge(domain_spec=plastics_domain_spec)
        catalog = bridge.get_rule_catalog()
        for entry in catalog:
            assert "name" in entry
            assert "requires" in entry
            assert "produces" in entry
            assert isinstance(entry["requires"], list)
            assert isinstance(entry["produces"], list)

    def test_no_domain_spec_run_returns_empty(self, flat_rules, partial_entity):
        bridge = InferenceBridge(rules=flat_rules)
        result = bridge.run(partial_entity)
        assert result.derived_fields == {}
        assert result.rules_fired == 0

    def test_no_domain_spec_catalog_uses_flat_rules(self, flat_rules):
        bridge = InferenceBridge(rules=flat_rules)
        catalog = bridge.get_rule_catalog()
        assert len(catalog) == 1
        assert catalog[0]["name"] == "premium_hdpe"


# ── v2 Feature Tests ─────────────────────────────────────


class TestV2FeatureExposure:
    """New capabilities available through the adapter."""

    def test_unlock_map_populated_on_partial_entity(self, plastics_domain_spec, partial_entity):
        bridge = InferenceBridge(domain_spec=plastics_domain_spec)
        result = bridge.run(
            partial_entity,
            {"polymer_type": 0.9, "contamination_pct": 0.8},
        )
        assert isinstance(result.unlock_map, dict)

    def test_blocked_fields_populated(self, plastics_domain_spec, partial_entity):
        bridge = InferenceBridge(domain_spec=plastics_domain_spec)
        result = bridge.run(
            partial_entity,
            {"polymer_type": 0.9, "contamination_pct": 0.8},
        )
        assert isinstance(result.blocked_fields, dict)

    def test_graph_accessible(self, plastics_domain_spec):
        bridge = InferenceBridge(domain_spec=plastics_domain_spec)
        assert bridge.graph is not None
        assert bridge.graph.domain == "plastics_recycling"

    def test_graph_edges_match_derived_from(self, plastics_domain_spec):
        bridge = InferenceBridge(domain_spec=plastics_domain_spec)
        targets = {e.target for e in bridge.graph.edges}
        assert "material_grade" in targets
        assert "facility_tier" in targets


# ── Catalog Parity Tests ─────────────────────────────────


class TestCatalogParity:
    """Catalog from graph should match what planner expects."""

    def test_graph_catalog_has_material_grade_rule(self, plastics_domain_spec):
        bridge = InferenceBridge(domain_spec=plastics_domain_spec)
        catalog = bridge.get_rule_catalog()
        names = [r["name"] for r in catalog]
        assert "material_grade_lookup" in names

    def test_graph_catalog_requires_correct_inputs(self, plastics_domain_spec):
        bridge = InferenceBridge(domain_spec=plastics_domain_spec)
        catalog = bridge.get_rule_catalog()
        grade_rule = next(r for r in catalog if r["name"] == "material_grade_lookup")
        assert set(grade_rule["requires"]) == {
            "polymer_type",
            "contamination_pct",
        }
        assert grade_rule["produces"] == ["material_grade"]

    def test_graph_catalog_multi_hop_ordering(self, plastics_domain_spec):
        bridge = InferenceBridge(domain_spec=plastics_domain_spec)
        catalog = bridge.get_rule_catalog()
        names = [r["name"] for r in catalog]
        idx_grade = names.index("material_grade_lookup")
        idx_tier = names.index("facility_tier_compute")
        assert idx_grade < idx_tier


# ── Domain `inference_rules` (condition rules) ───────────


@pytest.fixture
def condition_rules():
    """The production PlasticOS rule shape: conditions → literal outputs."""
    return [
        {
            "rule_id": "grade-a",
            "conditions": [
                {"field": "polymer_types", "operator": "CONTAINS", "value": "HDPE"},
                {"field": "contamination_tolerance", "operator": "LT", "value": 0.03},
            ],
            "outputs": [
                {"field": "material_grade", "value_expr": "A", "derivation_type": "classification"}
            ],
            "confidence": 0.92,
            "priority": 10,
            "rule_or_model_version": "v-test",
        },
        {
            "rule_id": "tier-1",
            "conditions": [
                {"field": "annual_capacity_mt", "operator": "GTE", "value": 5000},
                {"field": "material_grade", "operator": "IN", "value": ["A", "A+"]},
            ],
            "outputs": [
                {
                    "field": "facility_tier",
                    "value_expr": "tier_1",
                    "derivation_type": "classification",
                }
            ],
            "confidence": 0.90,
            "priority": 8,
        },
    ]


@pytest.fixture
def evidence():
    return {
        "polymer_types": ["HDPE"],
        "contamination_tolerance": 0.01,
        "annual_capacity_mt": 7000,
    }


class TestConditionRules:
    def test_status_and_catalog(self, condition_rules):
        bridge = InferenceBridge(rules=condition_rules)
        assert bridge.status == "active"
        assert bridge.describe()["engines"] == ["condition_rules"]
        assert bridge.describe()["rule_versions"] == ["v-test"]
        catalog = {r["name"]: r for r in bridge.get_rule_catalog()}
        assert catalog["tier-1"]["requires"] == ["annual_capacity_mt", "material_grade"]

    def test_multi_hop_with_bounded_confidence(self, condition_rules, evidence):
        conf = {"polymer_types": 0.95, "contamination_tolerance": 0.80, "annual_capacity_mt": 0.99}
        result = InferenceBridge(rules=condition_rules).run(evidence, conf)

        assert result.derived_fields == {"material_grade": "A", "facility_tier": "tier_1"}
        assert result.confidence_map["material_grade"] == pytest.approx(0.80)
        assert result.confidence_map["facility_tier"] == pytest.approx(0.80)
        assert result.rules_fired == 2
        assert result.provenance["facility_tier"]["inputs"] == {
            "annual_capacity_mt": 7000,
            "material_grade": "A",
        }
        assert result.provenance["material_grade"]["rule_version"] == "v-test"

    def test_record_supplied_inputs_bound_nothing(self, condition_rules, evidence):
        result = InferenceBridge(rules=condition_rules).run(evidence, {})
        assert result.confidence_map["material_grade"] == pytest.approx(0.92)

    def test_present_field_is_not_overwritten(self, condition_rules, evidence):
        result = InferenceBridge(rules=condition_rules).run(
            {**evidence, "material_grade": "B"}, {"material_grade": 0.3}
        )
        assert "material_grade" not in result.derived_fields
        assert "facility_tier" not in result.derived_fields  # "B" fails tier-1

    def test_rerun_on_own_output_derives_nothing_new(self, condition_rules, evidence):
        bridge = InferenceBridge(rules=condition_rules)
        first = bridge.run(evidence, {})
        second = bridge.run({**evidence, **first.derived_fields}, dict(first.confidence_map))
        assert second.derived_fields == {}

    def test_invalid_rule_refuses_whole_set(self, condition_rules, evidence):
        condition_rules[1]["outputs"][0]["derivation_type"] = "guess"
        bridge = InferenceBridge(rules=condition_rules)
        assert bridge.status == "invalid_rules"
        assert "tier-1" in bridge.describe()["errors"][0]
        assert bridge.run(evidence, {}).derived_fields == {}

    def test_flat_v1_rules_stay_catalog_only(self, flat_rules):
        bridge = InferenceBridge(rules=flat_rules)
        assert bridge.status == "catalog_only"
        assert bridge.run({"polymer_type": "HDPE"}, {}).derived_fields == {}

    def test_no_context_is_reported(self):
        assert InferenceBridge().describe()["status"] == "not_configured"


class TestConditionRuleUnlockMap:
    """#237: condition rules inform search targeting through unlock_map."""

    def test_sole_missing_condition_field_is_credited(self, condition_rules):
        bridge = InferenceBridge(rules=condition_rules)
        result = bridge.run({"polymer_types": ["HDPE"], "annual_capacity_mt": 7000}, {})

        # grade-a needs only contamination_tolerance; tier-1 needs grade-a's output.
        assert result.unlock_map == {"contamination_tolerance": 1.0, "material_grade": 1.0}
        assert bridge.unlock_map == result.unlock_map

    def test_a_present_condition_that_fails_earns_no_credit(self, condition_rules):
        result = InferenceBridge(rules=condition_rules).run({"polymer_types": ["PP"]}, {})
        assert "contamination_tolerance" not in result.unlock_map

    def test_two_missing_condition_fields_earn_no_credit(self, condition_rules):
        result = InferenceBridge(rules=condition_rules).run({}, {})
        assert result.unlock_map == {}

    def test_a_rule_whose_output_is_known_earns_no_credit(self, condition_rules):
        result = InferenceBridge(rules=condition_rules).run(
            {"polymer_types": ["HDPE"], "material_grade": "B"}, {}
        )
        assert "contamination_tolerance" not in result.unlock_map

    def test_registry_is_exposed_for_the_planner(self, condition_rules):
        bridge = InferenceBridge(rules=condition_rules)
        assert bridge.rule_registry is not None
        assert {r.rule_id for r in bridge.rule_registry.all_rules()} == {"grade-a", "tier-1"}
        assert InferenceBridge().rule_registry is None
