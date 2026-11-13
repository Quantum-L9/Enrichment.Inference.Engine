"""Tests for app/engines/search_optimizer.py — validates decision paths."""

from __future__ import annotations

from unittest.mock import patch

from app.engines.field_classifier import DomainClassification
from app.engines.search_optimizer import (
    EntitySignals,
    FieldDifficulty,
    MessageStrategy,
    SearchContextSize,
    SonarConfig,
    SonarModel,
    estimate_call_cost,
    resolve,
    resolve_from_classification,
)

SAMPLE_FIELD_MAP = {
    "name": FieldDifficulty.TRIVIAL,
    "website": FieldDifficulty.TRIVIAL,
    "revenue": FieldDifficulty.PUBLIC,
    "employee_count": FieldDifficulty.PUBLIC,
    "certifications": FieldDifficulty.FINDABLE,
    "annual_capacity_tons": FieldDifficulty.OBSCURE,
    "material_grade": FieldDifficulty.INFERRABLE,
    "facility_tier": FieldDifficulty.INFERRABLE,
}


class TestResolveBasic:
    """Test the core resolve() function."""

    def test_all_inferrable_returns_disabled(self):
        """When all target fields are inferrable, search should be disabled."""
        config = resolve(
            search_plan_mode="targeted",
            target_fields=["material_grade", "facility_tier"],
            signals=EntitySignals(known_count=5, pass_number=2),
            field_map=SAMPLE_FIELD_MAP,
        )
        assert config.disable_search is True
        assert config.variations == 0
        assert config.estimated_cost_per_call == 0.0

    def test_discovery_uses_sonar_pro(self):
        """Discovery mode should select sonar-pro for broad search."""
        config = resolve(
            search_plan_mode="discovery",
            target_fields=["revenue", "certifications", "annual_capacity_tons"],
            signals=EntitySignals(null_count=8, known_count=2, pass_number=1),
            field_map=SAMPLE_FIELD_MAP,
        )
        assert config.model == SonarModel.SONAR_PRO
        assert config.search_context_size == SearchContextSize.HIGH

    def test_targeted_trivial_uses_sonar_base(self):
        """Targeting trivial/public fields should use base sonar."""
        config = resolve(
            search_plan_mode="targeted",
            target_fields=["revenue", "employee_count"],
            signals=EntitySignals(known_count=5, pass_number=2),
            field_map=SAMPLE_FIELD_MAP,
        )
        assert config.model == SonarModel.SONAR

    def test_verification_low_context(self):
        """Verification mode should use low context size."""
        config = resolve(
            search_plan_mode="verification",
            target_fields=["certifications"],
            signals=EntitySignals(known_count=10, pass_number=3),
            field_map=SAMPLE_FIELD_MAP,
        )
        assert config.search_context_size == SearchContextSize.LOW

    def test_obscure_uses_high_context(self):
        """Obscure fields should trigger high context size."""
        config = resolve(
            search_plan_mode="targeted",
            target_fields=["annual_capacity_tons"],
            signals=EntitySignals(known_count=5, pass_number=2),
            field_map=SAMPLE_FIELD_MAP,
        )
        assert config.search_context_size in (SearchContextSize.HIGH, SearchContextSize.MEDIUM)

    def test_message_strategy_targeted_with_known(self):
        """Targeted mode with >5 known fields should use assistant strategy."""
        config = resolve(
            search_plan_mode="targeted",
            target_fields=["certifications"],
            signals=EntitySignals(known_count=8, pass_number=2),
            field_map=SAMPLE_FIELD_MAP,
        )
        assert config.message_strategy == MessageStrategy.SYSTEM_USER_ASSISTANT

    def test_force_model_override(self):
        """force_model should override all selection logic."""
        config = resolve(
            search_plan_mode="discovery",
            target_fields=["revenue"],
            signals=EntitySignals(pass_number=1),
            field_map=SAMPLE_FIELD_MAP,
            force_model=SonarModel.SONAR_REASONING,
        )
        assert config.model == SonarModel.SONAR_REASONING

    def test_logs_resolution_reason_as_extra_field(self):
        """The stdlib logger retains the reason as structured record metadata."""
        with patch("app.engines.search_optimizer.logger.info") as mock_info:
            config = resolve(
                search_plan_mode="targeted",
                target_fields=["revenue"],
                signals=EntitySignals(known_count=5, pass_number=2),
                field_map=SAMPLE_FIELD_MAP,
            )

        mock_info.assert_called_once_with(
            "search_optimizer.resolve",
            extra={"reason": config.resolution_reason},
        )


class TestResolveFromClassification:
    """Test the convenience resolve_from_classification wrapper."""

    def test_unpacks_classification(self):
        """resolve_from_classification should work with DomainClassification."""
        classification = DomainClassification(
            domain="test",
            field_map=SAMPLE_FIELD_MAP,
            domain_filters={},
            gate_fields={"certifications"},
            scoring_fields=set(),
            time_sensitive_fields=set(),
            ambiguous_fields=set(),
            stats={"trivial": 2, "public": 2, "findable": 1, "obscure": 1, "inferrable": 2},
        )
        signals = EntitySignals(known_count=3, null_count=5, pass_number=1)
        config = resolve_from_classification(
            search_plan_mode="discovery",
            target_fields=["revenue", "certifications"],
            signals=signals,
            classification=classification,
        )
        assert isinstance(config, SonarConfig)
        assert config.model in SonarModel


class TestCostEstimation:
    """Test cost model accuracy."""

    def test_sonar_base_cost(self):
        cost = estimate_call_cost(SonarModel.SONAR, SearchContextSize.MEDIUM, 2048, 3)
        assert cost > 0
        assert cost < 0.1  # sanity

    def test_sonar_pro_more_expensive(self):
        base = estimate_call_cost(SonarModel.SONAR, SearchContextSize.MEDIUM, 2048, 3)
        pro = estimate_call_cost(SonarModel.SONAR_PRO, SearchContextSize.MEDIUM, 2048, 3)
        assert pro > base

    def test_high_context_more_expensive(self):
        low = estimate_call_cost(SonarModel.SONAR, SearchContextSize.LOW, 2048, 3)
        high = estimate_call_cost(SonarModel.SONAR, SearchContextSize.HIGH, 2048, 3)
        assert high > low


class TestSonarConfigSerialization:
    """Test to_dict and to_api_params."""

    def test_to_dict_complete(self):
        config = SonarConfig(
            model=SonarModel.SONAR_PRO,
            estimated_cost_per_call=0.05,
        )
        d = config.to_dict()
        assert d["model"] == "sonar-pro"
        assert "estimated_cost_per_call" in d
        assert "resolution_reason" in d

    def test_to_api_params(self):
        config = SonarConfig(model=SonarModel.SONAR)
        params = config.to_api_params()
        assert params["model"] == "sonar"
        assert "web_search_options" in params


# ── #320 review F1: the optimizer recommends, the pass budget authorizes ─────


def test_cap_variations_bounds_count_and_recomputes_cost() -> None:
    from app.engines.search_optimizer import cap_variations

    config = SonarConfig(
        model=SonarModel.SONAR_PRO,
        search_context_size=SearchContextSize.HIGH,
        max_tokens=2048,
        variations=4,
        estimated_cost_per_call=estimate_call_cost(
            SonarModel.SONAR_PRO, SearchContextSize.HIGH, 2048, 4
        ),
        resolution_reason="mode=discovery",
    )
    capped = cap_variations(config, 2)
    assert capped.variations == 2
    assert capped.estimated_cost_per_call == estimate_call_cost(
        SonarModel.SONAR_PRO, SearchContextSize.HIGH, 2048, 2
    )
    assert capped.resolution_reason.endswith("capped×2")
    # The original is not mutated, and a cap above the count changes nothing.
    assert config.variations == 4
    assert cap_variations(config, 7) is config


# ── Post-#320 F2: the planner owns the convergence variation count ───────────


def _findable_resolve(**kwargs):
    from app.engines.field_classifier import FieldDifficulty
    from app.engines.search_optimizer import resolve

    defaults = {
        "search_plan_mode": "verification",
        "target_fields": ["street"],
        "signals": EntitySignals(avg_confidence=0.9, allocated_tokens=60000),
        "field_map": {"street": FieldDifficulty.TRIVIAL},
    }
    defaults.update(kwargs)
    return resolve(**defaults)


def test_f2_01_authorized_count_is_not_undercut_by_the_optimizer() -> None:
    # Unauthorized, this verification/trivial/high-confidence pass recommends 2.
    assert _findable_resolve().variations == 2
    config = _findable_resolve(authorized_variations=5)
    assert config.variations == 5


def test_f2_02_authorized_count_is_not_raised_by_the_optimizer() -> None:
    from app.engines.field_classifier import FieldDifficulty

    obscure = {
        "search_plan_mode": "discovery",
        "field_map": {"street": FieldDifficulty.OBSCURE},
        "signals": EntitySignals(avg_confidence=0.1, allocated_tokens=60000),
    }
    assert _findable_resolve(**obscure).variations > 2
    assert _findable_resolve(**obscure, authorized_variations=2).variations == 2


def test_f2_11_cost_estimate_is_for_the_authorized_count() -> None:
    config = _findable_resolve(authorized_variations=5)
    assert config.estimated_cost_per_call == estimate_call_cost(
        config.model, config.search_context_size, config.max_tokens, 5
    )


def test_f2_06_zero_budget_is_not_replaced_by_allocated_tokens() -> None:
    signals = EntitySignals(avg_confidence=0.5, allocated_tokens=60000)
    assert _findable_resolve(search_plan_mode="discovery", signals=signals).variations == 3
    # A real zero budget bounds the recommendation; `or` used to swap in 60000.
    zero = _findable_resolve(search_plan_mode="discovery", signals=signals, budget_tokens=0)
    assert zero.variations == 2


def test_f2_05_zero_authorization_is_a_real_zero() -> None:
    config = _findable_resolve(authorized_variations=0)
    assert config.variations == 0
    assert config.estimated_cost_per_call == 0.0


def test_inferrable_fields_still_disable_search_whatever_is_authorized() -> None:
    from app.engines.field_classifier import FieldDifficulty

    config = _findable_resolve(
        field_map={"street": FieldDifficulty.INFERRABLE}, authorized_variations=5
    )
    assert config.disable_search is True
    assert config.variations == 0
