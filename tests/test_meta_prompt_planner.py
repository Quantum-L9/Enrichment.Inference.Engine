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

    def test_no_domain_spec_uses_default_field_metadata(self) -> None:
        gaps = MetaPromptPlanner()._identify_missing_fields({"facility_type": None}, {})

        assert len(gaps) == 1
        assert gaps[0].is_gate_critical is False
        assert gaps[0].scoring_weight == 0.0

    def test_raw_ontology_mapping_preserves_gate_and_scoring_metadata(self) -> None:
        planner = MetaPromptPlanner(
            {
                "ontology": {
                    "nodes": {
                        "Facility": {
                            "properties": {
                                "facility_type": {
                                    "metadata": {
                                        "gate_critical": True,
                                        "scoring_weight": "0.25",
                                    }
                                },
                                "annual_capacity_mt": {"metadata": {"scoring_weight": 0.75}},
                            }
                        }
                    }
                }
            }
        )

        gaps = planner._identify_missing_fields(
            {"facility_type": None, "annual_capacity_mt": None}, {}
        )
        gaps_by_name = {gap.field_name: gap for gap in gaps}

        assert gaps_by_name["facility_type"].is_gate_critical is True
        assert gaps_by_name["facility_type"].scoring_weight == 0.25
        assert gaps_by_name["annual_capacity_mt"].scoring_weight == 0.75
        assert planner._rank_fields(gaps, "targeted") == [
            "facility_type",
            "annual_capacity_mt",
        ]

    def test_raw_ontology_list_preserves_gate_and_scoring_metadata(self) -> None:
        planner = MetaPromptPlanner(
            {
                "ontology": {
                    "nodes": [
                        {
                            "label": "Facility",
                            "properties": [
                                {
                                    "name": "facility_type",
                                    "type": "string",
                                    "metadata": {
                                        "gate_critical": True,
                                        "scoring_weight": 0.25,
                                    },
                                },
                                {
                                    "name": "annual_capacity_mt",
                                    "type": "float",
                                    "scoring_weight": 0.75,
                                },
                            ],
                        }
                    ]
                }
            }
        )

        gaps = planner._identify_missing_fields(
            {"facility_type": None, "annual_capacity_mt": None}, {}
        )
        gaps_by_name = {gap.field_name: gap for gap in gaps}

        assert gaps_by_name["facility_type"].is_gate_critical is True
        assert gaps_by_name["facility_type"].scoring_weight == 0.25
        assert gaps_by_name["annual_capacity_mt"].scoring_weight == 0.75

    @pytest.mark.parametrize(
        "malformed_spec",
        [
            {},
            {"ontology": {"nodes": []}},
            {"ontology": {"nodes": {"Facility": {"properties": []}}}},
            {"ontology": {"nodes": {"Facility": {"properties": {"facility_type": None}}}}},
        ],
    )
    def test_malformed_raw_domain_spec_uses_default_field_metadata(
        self, malformed_spec: dict[str, object]
    ) -> None:
        gaps = MetaPromptPlanner(malformed_spec)._identify_missing_fields(
            {"facility_type": None}, {}
        )

        assert len(gaps) == 1
        assert gaps[0].is_gate_critical is False
        assert gaps[0].scoring_weight == 0.0

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


# ── D-9 v2: threshold-aware targeting and U_action budgeting (issue #266) ──


def _plan_v2(pass_number: int = 2, uncertainty: float = 1.0, **kwargs):
    planner_kwargs = {
        "confidence_threshold": kwargs.pop("confidence_threshold", 0.85),
        "priority_fields": kwargs.pop("priority_fields", None),
    }
    if "domain_spec" in kwargs:
        planner_kwargs["domain_spec"] = kwargs.pop("domain_spec")
    return MetaPromptPlanner(**planner_kwargs).plan_pass(
        pass_context=_ctx(pass_number), uncertainty_score=uncertainty, **kwargs
    )


def test_default_threshold_targets_every_field_below_it() -> None:
    """T03: at 0.85, both author (0.30) and date (0.80) are targets, not just author."""
    plan = _plan_v2(
        uncertainty=7.0,
        entity_state={"title": "T", "author": "A", "date": "2026"},
        field_confidences={"title": 0.95, "author": 0.30, "date": 0.80},
        required_fields=["title", "author", "date"],
    )
    assert set(plan.priority_fields) == {"author", "date"}
    assert plan.mode == "targeted"


def test_stricter_consumer_threshold_keeps_a_low_u_field_targeted() -> None:
    """T04: confidence 0.90 under a 0.95 bar is U_action 1, yet still a target."""
    plan = _plan_v2(
        uncertainty=1.0,
        entity_state={"street": "1 Main St"},
        field_confidences={"street": 0.90},
        required_fields=["street"],
        field_thresholds={"street": 0.95},
    )
    assert plan.mode == "targeted"
    assert plan.priority_fields == ["street"]
    assert plan.variation_count == 2  # U_action 1 keeps the small budget


def test_boundary_u_of_two_does_not_suppress_targeting() -> None:
    """T05: confidence 0.80 under 0.85 is U_action 2.0, and still targeted."""
    plan = _plan_v2(
        uncertainty=10 * (1 - 0.80),
        entity_state={"industry": "Recycling"},
        field_confidences={"industry": 0.80},
        required_fields=["industry"],
    )
    assert plan.mode == "targeted"
    assert plan.priority_fields == ["industry"]


@pytest.mark.parametrize(
    ("confidence", "expected_budget"),
    [(0.50, 3), (0.49, 5), (0.81, 2)],
)
def test_variation_budget_follows_u_action(confidence: float, expected_budget: int) -> None:
    """T06/T07: U_action 5 is the medium budget; 5.1 is eligible for the high one."""
    plan = _plan_v2(
        uncertainty=10 * (1 - confidence),
        entity_state={"industry": "Recycling"},
        field_confidences={"industry": confidence},
        required_fields=["industry"],
    )
    assert plan.variation_count == expected_budget


def test_low_u_action_with_every_field_ready_is_verification() -> None:
    plan = _plan_v2(
        uncertainty=1.0,
        entity_state={"street": "1 Main St"},
        field_confidences={"street": 0.97},
        required_fields=["street"],
        field_thresholds={"street": 0.95},
    )
    assert plan.mode == "verification"
    assert plan.priority_fields == []


def test_schema_free_deciding_gap_keeps_the_pass_targeted() -> None:
    plan = _plan_v2(
        uncertainty=1.8,
        entity_state={"industry": "Recycling"},
        field_confidences={"industry": 0.82},
        deciding_fields=["industry"],
    )
    assert plan.mode == "targeted"


def test_pass_one_is_discovery_whatever_the_gaps() -> None:
    plan = _plan_v2(
        pass_number=1,
        uncertainty=10.0,
        entity_state={"name": "Acme"},
        field_confidences={},
        required_fields=["street"],
    )
    assert plan.mode == "discovery"
    assert "street" in plan.priority_fields


def test_required_field_absent_from_the_entity_is_a_gap() -> None:
    """T22: a requested field the entity does not carry yet is targeted."""
    plan = _plan_v2(
        uncertainty=10.0,
        entity_state={"name": "Acme"},
        field_confidences={"name": 0.95},
        required_fields=["website"],
    )
    assert plan.priority_fields == ["website"]


def test_hints_still_lead_within_the_required_group() -> None:
    """T23: domain hints keep their order inside the required group."""
    plan = _plan_v2(
        uncertainty=10.0,
        entity_state={"name": "Acme", "notes": None},
        field_confidences={"name": 0.95},
        required_fields=["street", "website"],
        priority_fields=["website"],
    )
    assert plan.priority_fields[:2] == ["website", "street"]
    assert plan.priority_fields[-1] == "notes"


def test_gate_critical_ranking_is_kept_within_required_fields() -> None:
    """T23: gate-critical required gaps outrank other required gaps."""
    spec = {
        "ontology": {
            "nodes": {
                "Company": {
                    "properties": {
                        "street": {"metadata": {"gate_critical": False}},
                        "facility_type": {"metadata": {"gate_critical": True}},
                    }
                }
            }
        }
    }
    plan = _plan_v2(
        uncertainty=10.0,
        entity_state={},
        field_confidences={},
        required_fields=["street", "facility_type"],
        domain_spec=spec,
    )
    assert plan.priority_fields == ["facility_type", "street"]


def test_top_ten_never_drops_a_required_gap() -> None:
    """T24: with more than ten gaps, required ones survive the cut, deterministically."""
    entity = {f"optional_{i:02d}": None for i in range(12)}
    kwargs = {
        "uncertainty": 10.0,
        "entity_state": entity,
        "field_confidences": {},
        "required_fields": ["street", "website"],
    }
    first = _plan_v2(**kwargs)
    second = _plan_v2(**kwargs)
    assert len(first.priority_fields) == 10
    # Both required gaps lead; the existing ranking orders them within the group.
    assert set(first.priority_fields[:2]) == {"street", "website"}
    assert first.priority_fields == second.priority_fields


def test_missing_value_with_reported_confidence_is_targeted() -> None:
    """T10 at the planner: an empty value at 0.99 is still a gap."""
    plan = _plan_v2(
        uncertainty=1.0,
        entity_state={"street": ""},
        field_confidences={"street": 0.99},
        required_fields=["street"],
    )
    assert plan.mode == "targeted"
    assert plan.priority_fields == ["street"]


def test_zero_and_false_values_are_not_gaps() -> None:
    """T11 at the planner."""
    plan = _plan_v2(
        uncertainty=1.0,
        entity_state={"employees": 0, "is_public": False},
        field_confidences={"employees": 0.9, "is_public": 0.9},
        required_fields=["employees", "is_public"],
    )
    assert plan.priority_fields == []
    assert plan.mode == "verification"
