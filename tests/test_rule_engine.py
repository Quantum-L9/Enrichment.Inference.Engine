"""Tests for app/engines/inference/rule_engine.py

Covers: Deterministic inference rule evaluation against entity fields.

Source: 195 lines | Target coverage: 85%
"""

from __future__ import annotations

import pytest

from app.engines.inference.rule_engine import infer
from app.engines.inference.rule_loader import (
    Operator,
    RuleCondition,
    RuleDefinition,
    RuleOutput,
    RuleRegistry,
)


def _grade_rule(
    rule_id: str,
    contamination_ops: list[tuple[Operator, float]],
    value: str,
    confidence: float,
    priority: int,
) -> RuleDefinition:
    conditions = [
        RuleCondition(field="polymer_type", operator=Operator.EQUALS, value="HDPE"),
    ]
    for op, bound in contamination_ops:
        conditions.append(RuleCondition(field="contamination_pct", operator=op, value=bound))
    return RuleDefinition(
        rule_id=rule_id,
        conditions=conditions,
        outputs=[
            RuleOutput(
                field="material_grade",
                value_expr=value,
                derivation_type="classification",
            )
        ],
        confidence=confidence,
        priority=priority,
    )


class TestRuleEngine:
    """Tests for deterministic inference rule execution."""

    @pytest.fixture
    def hdpe_registry(self) -> RuleRegistry:
        registry = RuleRegistry()
        registry.add_rule(
            _grade_rule(
                "premium_hdpe_grade",
                [(Operator.LT, 2.0)],
                "Premium HDPE",
                0.95,
                1,
            )
        )
        registry.add_rule(
            _grade_rule(
                "standard_hdpe_grade",
                [(Operator.GTE, 2.0), (Operator.LT, 5.0)],
                "Standard HDPE",
                0.90,
                2,
            )
        )
        registry.add_rule(
            _grade_rule(
                "recycled_hdpe_grade",
                [(Operator.GTE, 5.0), (Operator.LT, 10.0)],
                "Recycled HDPE",
                0.85,
                3,
            )
        )
        return registry

    def test_rule_matching_simple_condition(self, hdpe_registry):
        entity = {"polymer_type": "HDPE", "contamination_pct": 1.0}
        result = infer(entity, hdpe_registry)
        assert "material_grade" in result.derived_fields
        assert result.derived_fields["material_grade"] == "Premium HDPE"

    def test_rule_matching_range_condition(self, hdpe_registry):
        entity = {"polymer_type": "HDPE", "contamination_pct": 3.5}
        result = infer(entity, hdpe_registry)
        assert result.derived_fields.get("material_grade") == "Standard HDPE"

    def test_rule_matching_multiple_conditions(self, hdpe_registry):
        entity = {"polymer_type": "HDPE", "contamination_pct": 7.0}
        result = infer(entity, hdpe_registry)
        assert result.derived_fields.get("material_grade") == "Recycled HDPE"

    def test_rule_execution_sets_field_value(self, hdpe_registry):
        entity = {"polymer_type": "HDPE", "contamination_pct": 1.0}
        result = infer(entity, hdpe_registry)
        assert result.derived_fields["material_grade"] == "Premium HDPE"

    def test_rule_confidence_propagation(self, hdpe_registry):
        entity = {"polymer_type": "HDPE", "contamination_pct": 1.0}
        result = infer(entity, hdpe_registry)
        assert result.inference_confidence > 0.0
        assert result.inference_confidence <= 0.95

    def test_no_matching_rules_returns_empty(self, hdpe_registry):
        entity = {"polymer_type": "PP", "contamination_pct": 1.0}
        result = infer(entity, hdpe_registry)
        assert len(result.derived_fields) == 0

    def test_rules_fired_tracking(self, hdpe_registry):
        entity = {"polymer_type": "HDPE", "contamination_pct": 1.0}
        result = infer(entity, hdpe_registry)
        fired_ids = [fired.rule_id for fired in result.rules_fired]
        assert "premium_hdpe_grade" in fired_ids

    def test_missing_field_no_match(self, hdpe_registry):
        entity = {"polymer_type": "HDPE"}  # no contamination_pct
        result = infer(entity, hdpe_registry)
        assert "material_grade" not in result.derived_fields
