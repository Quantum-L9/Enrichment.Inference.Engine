"""Tests for app/engines/inference/rule_loader.py against RuleRegistry."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.engines.inference.rule_loader import RuleRegistry, load_rules, load_rules_data


class TestRuleLoader(unittest.TestCase):
    def test_load_rules_from_plasticos_spec(self):
        spec = Path("domains/plasticos/spec.yaml")
        registry = load_rules(spec)
        self.assertIsInstance(registry, RuleRegistry)
        self.assertGreaterEqual(registry.count(), 3)
        self.assertIsNotNone(registry.get("grade-assignment-hdpe-a"))

    def test_load_rules_data_builds_registry(self):
        registry = load_rules_data(
            [
                {
                    "rule_id": "valid",
                    "conditions": [{"field": "x", "operator": "EQUALS", "value": "y"}],
                    "outputs": [
                        {"field": "z", "value_expr": "w", "derivation_type": "classification"}
                    ],
                    "confidence": 0.9,
                },
                {"broken": True},
            ]
        )
        self.assertIsNotNone(registry.get("valid"))
        self.assertEqual(registry.count(), 1)

    def test_load_rules_data_rejects_empty_failures(self):
        with self.assertRaises(ValueError) as caught:
            load_rules_data([{"broken": True}])
        self.assertIn("All rules failed", str(caught.exception))
