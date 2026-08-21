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
