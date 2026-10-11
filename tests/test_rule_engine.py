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


class TestCascade:
    """A chained rule fires once, on the cascade after its input appears."""

    @pytest.fixture
    def chain_registry(self) -> RuleRegistry:
        registry = RuleRegistry()
        registry.add_rule(_grade_rule("premium_hdpe_grade", [(Operator.LT, 2.0)], "A", 0.95, 1))
        registry.add_rule(
            RuleDefinition(
                rule_id="tier_from_grade",
                conditions=[
                    RuleCondition(field="material_grade", operator=Operator.IN, value=["A"])
                ],
                outputs=[
                    RuleOutput(
                        field="facility_tier",
                        value_expr="tier_1",
                        derivation_type="classification",
                    )
                ],
                confidence=0.9,
            )
        )
        return registry

    def test_each_rule_fires_once_with_its_cascade_level(self, chain_registry):
        result = infer({"polymer_type": "HDPE", "contamination_pct": 1.0}, chain_registry)
        levels = {f.rule_id: f.cascade_level for f in result.rules_fired}
        assert len(result.rules_fired) == 2
        assert levels == {"premium_hdpe_grade": 0, "tier_from_grade": 1}
        assert result.cascade_depth == 2
        assert result.derived_fields == {"material_grade": "A", "facility_tier": "tier_1"}

    def test_no_fire_reports_zero_depth(self, chain_registry):
        result = infer({"polymer_type": "PP", "contamination_pct": 1.0}, chain_registry)
        assert result.rules_fired == []
        assert result.cascade_depth == 0

    def test_later_cascade_does_not_replace_a_consumed_value(self):
        def _rule(rule_id, conditions, out_field, value, priority):
            return RuleDefinition(
                rule_id=rule_id,
                conditions=[RuleCondition(field=f, operator=Operator.EXISTS) for f in conditions],
                outputs=[
                    RuleOutput(field=out_field, value_expr=value, derivation_type="classification")
                ],
                confidence=0.9,
                priority=priority,
            )

        registry = RuleRegistry()
        registry.add_rule(_rule("grade_low", ["polymer_type"], "material_grade", "B", 1))
        registry.add_rule(_rule("certified", ["polymer_type"], "certified", True, 1))
        # Higher priority, but only satisfiable on cascade 1 — after tier consumed "B".
        registry.add_rule(_rule("grade_high", ["certified"], "material_grade", "A", 9))
        registry.add_rule(
            RuleDefinition(
                rule_id="tier",
                conditions=[
                    RuleCondition(field="material_grade", operator=Operator.EQUALS, value="B")
                ],
                outputs=[
                    RuleOutput(
                        field="facility_tier", value_expr="t2", derivation_type="classification"
                    )
                ],
                confidence=0.9,
            )
        )
        result = infer({"polymer_type": "HDPE"}, registry)
        assert result.derived_fields["material_grade"] == "B"
        assert result.derived_fields["facility_tier"] == "t2"
        assert result.derivation_chains["material_grade"] == ["grade_low"]
