"""Tests for app/engines/inference/rule_engine.py against RuleRegistry."""

from __future__ import annotations

import unittest

from app.engines.inference.rule_engine import infer
from app.engines.inference.rule_loader import load_rules_data


def _hdpe_registry():
    return load_rules_data(
        [
            {
                "rule_id": "premium_hdpe_grade",
                "conditions": [
                    {"field": "polymer_type", "operator": "EQUALS", "value": "HDPE"},
                    {"field": "contamination_pct", "operator": "LT", "value": 2.0},
                ],
                "outputs": [
                    {
                        "field": "material_grade",
                        "value_expr": "Premium HDPE",
                        "derivation_type": "classification",
                    }
                ],
                "confidence": 0.95,
                "priority": 10,
            },
            {
                "rule_id": "standard_hdpe_grade",
                "conditions": [
                    {"field": "polymer_type", "operator": "EQUALS", "value": "HDPE"},
                    {"field": "contamination_pct", "operator": "GTE", "value": 2.0},
                    {"field": "contamination_pct", "operator": "LT", "value": 5.0},
                ],
                "outputs": [
                    {
                        "field": "material_grade",
                        "value_expr": "Standard HDPE",
                        "derivation_type": "classification",
                    }
                ],
                "confidence": 0.90,
                "priority": 8,
            },
            {
                "rule_id": "recycled_hdpe_grade",
                "conditions": [
                    {"field": "polymer_type", "operator": "EQUALS", "value": "HDPE"},
                    {"field": "contamination_pct", "operator": "GTE", "value": 5.0},
                    {"field": "contamination_pct", "operator": "LT", "value": 10.0},
                ],
                "outputs": [
                    {
                        "field": "material_grade",
                        "value_expr": "Recycled HDPE",
                        "derivation_type": "classification",
                    }
                ],
                "confidence": 0.85,
                "priority": 5,
            },
        ]
    )


class TestRuleEngine(unittest.TestCase):
    def test_rule_matching_simple_condition(self):
        result = infer({"polymer_type": "HDPE", "contamination_pct": 1.0}, _hdpe_registry())
        self.assertEqual(result.derived_fields["material_grade"], "Premium HDPE")

    def test_rule_matching_range_condition(self):
        result = infer({"polymer_type": "HDPE", "contamination_pct": 3.5}, _hdpe_registry())
        self.assertEqual(result.derived_fields.get("material_grade"), "Standard HDPE")

    def test_rule_matching_multiple_conditions(self):
        result = infer({"polymer_type": "HDPE", "contamination_pct": 7.0}, _hdpe_registry())
        self.assertEqual(result.derived_fields.get("material_grade"), "Recycled HDPE")

    def test_rule_confidence_propagation(self):
        result = infer({"polymer_type": "HDPE", "contamination_pct": 1.0}, _hdpe_registry())
        self.assertEqual(result.confidence_map.get("material_grade", 0.0), 0.95)

    def test_no_matching_rules_returns_empty(self):
        result = infer({"polymer_type": "PP", "contamination_pct": 1.0}, _hdpe_registry())
        self.assertEqual(result.derived_fields, {})

    def test_rules_fired_tracking(self):
        result = infer({"polymer_type": "HDPE", "contamination_pct": 1.0}, _hdpe_registry())
        self.assertIn("premium_hdpe_grade", {item.rule_id for item in result.rules_fired})

    def test_missing_field_no_match(self):
        result = infer({"polymer_type": "HDPE"}, _hdpe_registry())
        self.assertNotIn("material_grade", result.derived_fields)
