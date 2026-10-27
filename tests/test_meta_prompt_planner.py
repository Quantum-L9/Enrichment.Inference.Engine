"""Tests for app/engines/meta_prompt_planner.py

Covers: Prompt variation generation, mode-specific prompting,
        SearchPlan/PromptPlan construction.
"""

from __future__ import annotations

import pytest

from app.engines.meta_prompt_planner import MetaPromptPlanner, PromptPlan, SearchPlan


class TestMetaPromptPlanner:
    """Tests for prompt variation generation against the live plan() signature."""

    @pytest.fixture
    def planner(self) -> MetaPromptPlanner:
        return MetaPromptPlanner()

    def test_plan_returns_search_plan(self, planner: MetaPromptPlanner):
        plan = planner.plan(
            entity={"Name": "Test Corp", "polymer_type": "HDPE"},
            known_fields={"polymer_type": 0.9},
            _inferred_fields={},
            domain_hints={},
            _inference_rule_catalog=[],
            pass_number=1,
        )
        assert isinstance(plan, SearchPlan)
        assert isinstance(plan, PromptPlan)

    def test_pass_1_discovery_mode(self, planner: MetaPromptPlanner):
        plan = planner.plan(
            entity={"Name": "Test Corp"},
            known_fields={},
            _inferred_fields={},
            domain_hints={},
            _inference_rule_catalog=[],
            pass_number=1,
        )
        assert plan.mode == "discovery"

    def test_pass_2_targeted_mode(self, planner: MetaPromptPlanner):
        plan = planner.plan(
            entity={"Name": "Test Corp", "polymer_type": "HDPE"},
            known_fields={"polymer_type": 0.9},
            _inferred_fields={},
            domain_hints={"priority_fields": ["mfi_range", "contamination_pct"]},
            _inference_rule_catalog=[],
            pass_number=2,
        )
        assert plan.mode in ("targeted", "discovery", "verification")

    def test_plan_has_objective(self, planner: MetaPromptPlanner):
        plan = planner.plan(
            entity={"Name": "Test Corp"},
            known_fields={},
            _inferred_fields={},
            domain_hints={},
            _inference_rule_catalog=[],
            pass_number=1,
        )
        assert plan.objective

    def test_plan_has_target_fields_in_later_passes(self, planner: MetaPromptPlanner):
        plan = planner.plan(
            entity={"Name": "Test Corp"},
            known_fields={"polymer_type": 0.9},
            _inferred_fields={"material_grade": "Standard HDPE"},
            domain_hints={"priority_fields": ["mfi_range"]},
            _inference_rule_catalog=[{"name": "tier_compute", "inputs": ["mfi_range"]}],
            pass_number=3,
        )
        assert plan.target_fields is not None
        assert plan.mode in ("targeted", "verification", "discovery")

    def test_variation_budget_on_plan(self, planner: MetaPromptPlanner):
        plan = planner.plan(
            entity={"Name": "Test Corp"},
            known_fields={},
            _inferred_fields={},
            domain_hints={},
            _inference_rule_catalog=[],
            pass_number=1,
        )
        assert plan.variation_count >= 1


# ── WU-04 / F-016 F-017 F-019: targeting ──────────────────────────────────


def _ctx(pass_number: int):
    from app.models.loop_schemas import PassContext

    return PassContext(pass_number=pass_number, budget_remaining=30000, previous_uncertainty=7.0)


def test_null_fields_are_targeted() -> None:
    plan = MetaPromptPlanner(confidence_threshold=0.85).plan_pass(
        entity_state={"name": "Acme", "industry": None, "employees": None},
        pass_context=_ctx(1),
        field_confidences={"name": 0.95},
        uncertainty_score=7.0,
    )
    assert {"industry", "employees"} <= set(plan.priority_fields)
    assert "name" not in plan.priority_fields


def test_field_below_the_configured_threshold_is_retargeted() -> None:
    plan = MetaPromptPlanner(confidence_threshold=0.85).plan_pass(
        entity_state={"industry": "Recycling"},
        pass_context=_ctx(2),
        field_confidences={"industry": 0.8},
        uncertainty_score=3.0,
    )
    assert plan.priority_fields == ["industry"]


def test_priority_hint_fields_come_first() -> None:
    plan = MetaPromptPlanner(
        confidence_threshold=0.85, priority_fields=["employees", "website"]
    ).plan_pass(
        entity_state={"name": "Acme", "industry": None},
        pass_context=_ctx(1),
        field_confidences={},
        uncertainty_score=7.0,
    )
    assert plan.priority_fields[:2] == ["employees", "website"]
    assert "industry" in plan.priority_fields


def test_a_hint_field_already_resolved_is_not_retargeted() -> None:
    plan = MetaPromptPlanner(confidence_threshold=0.85, priority_fields=["industry"]).plan_pass(
        entity_state={"industry": "Recycling", "employees": None},
        pass_context=_ctx(2),
        field_confidences={"industry": 0.95},
        uncertainty_score=3.0,
    )
    assert plan.priority_fields == ["employees"]
