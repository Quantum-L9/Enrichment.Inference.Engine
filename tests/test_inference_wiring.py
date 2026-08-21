"""PlasticOS inference_rules reach RuleEngine through the live DomainYamlReader."""

from __future__ import annotations

import unittest

from app.engines.domain_yaml_reader import DomainYamlReader
from app.engines.inference_bridge_adapter import InferenceBridge


class TestInferenceWiring(unittest.TestCase):
    def test_plasticos_inline_rules_derive_grade_and_tier(self):
        reader = DomainYamlReader("domains")
        config = reader.load("plasticos")
        rules = config.raw_spec.get("inference_rules")
        self.assertIsInstance(rules, list)
        bridge = InferenceBridge(rules=rules)
        result = bridge.run(
            {
                "polymer_types": ["HDPE"],
                "contamination_tolerance": 0.02,
                "annual_capacity_mt": 6000,
            }
        )
        self.assertEqual(result.derived_fields["material_grade"], "A")
        self.assertEqual(result.confidence_map["material_grade"], 0.92)
        self.assertEqual(result.derived_fields["facility_tier"], "tier_1")
        self.assertEqual(result.unlock_map, {})
