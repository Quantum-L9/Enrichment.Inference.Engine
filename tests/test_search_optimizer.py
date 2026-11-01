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
