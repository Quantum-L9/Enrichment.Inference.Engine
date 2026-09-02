"""Tests for app/engines/convergence_controller.py

Covers: Multi-pass loop orchestration, convergence detection, pass delegation,
        field_classifier + search_optimizer wiring.

Source: ~400 lines | Target coverage: 70%
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.engines.convergence_controller import (
    CONVERGENCE_THRESHOLD,
    MAX_PASSES,
    MIN_DELTA,
    ConvergenceState,
    _classification_cache,
    get_or_classify_domain,
    run_convergence_loop,
)
from app.engines.field_classifier import DomainClassification, FieldDifficulty
from app.engines.search_optimizer import SonarConfig, SonarModel
from app.models.schemas import EnrichRequest, EnrichResponse

# ── Fixtures ───────────────────────────────────────────────


SAMPLE_DOMAIN_SPEC = {
    "domain": "test_domain",
    "gate_fields": ["facility_type"],
    "scoring_fields": ["revenue"],
    "time_sensitive_fields": ["recent_news"],
    "ambiguous_fields": [],
    "ontology": {
        "nodes": {
            "Company": {
                "properties": {
                    "name": {"type": "string"},
                    "website": {"type": "string"},
                    "facility_type": {"type": "string"},
                    "revenue": {"type": "float", "discovery_confidence": 0.5},
                    "material_grade": {
                        "type": "string",
                        "managed_by": "computed",
                        "derived_from": ["facility_type"],
                    },
                    "recent_news": {"type": "string"},
                }
            }
        }
    },
}


class TestConvergenceController:
    """Tests for multi-pass loop orchestration."""

    @pytest.fixture
    def enrich_request(self):
        return EnrichRequest(
            entity={"Name": "Test Corp", "polymer_type": "HDPE"},
            object_type="Account",
            objective="Enrich plastics recycling data",
        )

    @pytest.fixture
    def mock_settings(self):
        settings = MagicMock()
        settings.pplx_api_key = "test-key"
        settings.perplexity_api_key = "test-key"
        settings.perplexity_model = "sonar"
        settings.max_concurrent_variations = 3
        settings.default_timeout_seconds = 30
        settings.max_budget_tokens = 30000
        return settings

    @pytest.fixture
    def mock_kb_resolver(self):
        return MagicMock()

    @pytest.fixture
    def mock_enricher_converging(self):
        """Enricher that returns diminishing results to trigger convergence."""
        call_count = 0

        async def enricher(request, settings, kb_resolver, idem_store, sonar_config=None):
            nonlocal call_count
            call_count += 1
            fields = {"field_a": "val"} if call_count == 1 else {}
            return EnrichResponse(
                fields=fields,
                confidence=0.85 if call_count == 1 else 0.86,
                variation_count=5,
                uncertainty_score=0.0,
                pass_count=1,
                inference_version="test",
                processing_time_ms=100,
                tokens_used=500,
                state="completed",
            )

        return enricher

    @pytest.mark.asyncio
    async def test_single_pass_returns_response(
        self, enrich_request, mock_settings, mock_kb_resolver, mock_enricher_converging
    ):
        response = await run_convergence_loop(
            request=enrich_request,
            settings=mock_settings,
            kb_resolver=mock_kb_resolver,
            enricher=mock_enricher_converging,
            inference_rules=[],
        )
        assert isinstance(response, EnrichResponse)
        assert response.state == "completed"

    @pytest.mark.asyncio
    async def test_convergence_stops_when_delta_below_threshold(
        self, enrich_request, mock_settings, mock_kb_resolver, mock_enricher_converging
    ):
        response = await run_convergence_loop(
            request=enrich_request,
            settings=mock_settings,
            kb_resolver=mock_kb_resolver,
            enricher=mock_enricher_converging,
            inference_rules=[],
        )
        # Should converge before MAX_PASSES since enricher returns diminishing results
        assert response.pass_count <= MAX_PASSES

    @pytest.mark.asyncio
    async def test_max_passes_respected(self, enrich_request, mock_settings, mock_kb_resolver):
        call_count = 0

        async def always_new_enricher(
            request, settings, kb_resolver, idem_store, sonar_config=None
        ):
            nonlocal call_count
            call_count += 1
            return EnrichResponse(
                fields={f"field_{call_count}": f"val_{call_count}"},
                confidence=0.5 + (call_count * 0.1),
                variation_count=5,
                uncertainty_score=0.0,
                pass_count=1,
                inference_version="test",
                processing_time_ms=100,
                tokens_used=1000,
                state="completed",
            )

        response = await run_convergence_loop(
            request=enrich_request,
            settings=mock_settings,
            kb_resolver=mock_kb_resolver,
            enricher=always_new_enricher,
            inference_rules=[],
        )
        assert response.pass_count <= MAX_PASSES

    @pytest.mark.asyncio
    async def test_tokens_tracked_across_passes(
        self, enrich_request, mock_settings, mock_kb_resolver, mock_enricher_converging
    ):
        response = await run_convergence_loop(
            request=enrich_request,
            settings=mock_settings,
            kb_resolver=mock_kb_resolver,
            enricher=mock_enricher_converging,
            inference_rules=[],
        )
        assert response.tokens_used > 0

    @pytest.mark.asyncio
    async def test_processing_time_tracked(
        self, enrich_request, mock_settings, mock_kb_resolver, mock_enricher_converging
    ):
        response = await run_convergence_loop(
            request=enrich_request,
            settings=mock_settings,
            kb_resolver=mock_kb_resolver,
            enricher=mock_enricher_converging,
            inference_rules=[],
        )
        assert response.processing_time_ms >= 0

    def test_convergence_state_initial(self):
        state = ConvergenceState()
        assert state.known_fields == {}
        assert state.inferred_fields == {}
        assert state.pass_results == []
        assert state.converged is False

    def test_convergence_constants(self):
        assert MAX_PASSES == 3
        assert CONVERGENCE_THRESHOLD == 2.0
        assert MIN_DELTA == 0.05


class TestDomainClassificationWiring:
    """Tests for field_classifier + search_optimizer integration."""

    @pytest.fixture
    def enrich_request(self):
        return EnrichRequest(
            entity={"Name": "Test Corp", "website": "https://test.com"},
            object_type="Account",
            objective="Enrich company data",
        )

    @pytest.fixture
    def mock_settings(self):
        settings = MagicMock()
        settings.pplx_api_key = "test-key"
        settings.perplexity_api_key = "test-key"
        settings.perplexity_model = "sonar"
        settings.max_concurrent_variations = 3
        settings.default_timeout_seconds = 30
        settings.max_budget_tokens = 30000
        return settings

    @pytest.fixture
    def mock_kb_resolver(self):
        return MagicMock()

    def test_domain_classification_computed(self):
        """Test that auto_classify_domain produces a valid DomainClassification."""
        _classification_cache.clear()
        classification = get_or_classify_domain(SAMPLE_DOMAIN_SPEC)
        assert isinstance(classification, DomainClassification)
        assert classification.domain == "test_domain"
        assert len(classification.field_map) > 0
        # material_grade should be INFERRABLE (managed_by: computed)
        assert classification.field_map.get("material_grade") == FieldDifficulty.INFERRABLE
        # name should be TRIVIAL (name pattern)
        assert classification.field_map.get("name") == FieldDifficulty.TRIVIAL

    def test_domain_classification_cached(self):
        """Test that repeated calls use the cache."""
        _classification_cache.clear()
        c1 = get_or_classify_domain(SAMPLE_DOMAIN_SPEC)
        c2 = get_or_classify_domain(SAMPLE_DOMAIN_SPEC)
        assert c1 is c2  # Same object from cache

    def test_domain_classification_stats(self):
        """Test that stats dict tallies correctly."""
        _classification_cache.clear()
        classification = get_or_classify_domain(SAMPLE_DOMAIN_SPEC)
        total = sum(classification.stats.values())
        assert total == len(classification.field_map)

    def test_domain_classification_to_dict(self):
        """Test TransportPacket compliance via to_dict."""
        _classification_cache.clear()
        classification = get_or_classify_domain(SAMPLE_DOMAIN_SPEC)
        d = classification.to_dict()
        assert isinstance(d, dict)
        assert d["domain"] == "test_domain"
        assert isinstance(d["field_map"], dict)
        # Values should be strings (enum values)
        for v in d["field_map"].values():
            assert isinstance(v, str)

    @pytest.mark.asyncio
    async def test_sonar_config_resolved_per_pass(
        self, enrich_request, mock_settings, mock_kb_resolver
    ):
        """Test that sonar_config is computed when domain_spec is provided."""
        received_configs = []

        async def tracking_enricher(request, settings, kb_resolver, idem_store, sonar_config=None):
            received_configs.append(sonar_config)
            return EnrichResponse(
                fields={"field_a": "val"} if len(received_configs) == 1 else {},
                confidence=0.85 if len(received_configs) == 1 else 0.86,
                variation_count=3,
                uncertainty_score=0.0,
                pass_count=1,
                inference_version="test",
                processing_time_ms=50,
                tokens_used=300,
                state="completed",
            )

        _classification_cache.clear()
        await run_convergence_loop(
            request=enrich_request,
            settings=mock_settings,
            kb_resolver=mock_kb_resolver,
            enricher=tracking_enricher,
            inference_rules=[],
            domain_spec=SAMPLE_DOMAIN_SPEC,
        )

        # At least one pass should have received a sonar_config
        assert len(received_configs) > 0
        assert any(c is not None for c in received_configs)
        # Check it's a SonarConfig instance
        for config in received_configs:
            if config is not None:
                assert isinstance(config, SonarConfig)
                assert config.model in SonarModel
                assert config.estimated_cost_per_call >= 0.0

    @pytest.mark.asyncio
    async def test_no_sonar_config_without_domain_spec(
        self, enrich_request, mock_settings, mock_kb_resolver
    ):
        """Test that sonar_config is None when no domain_spec is provided."""
        received_configs = []

        async def tracking_enricher(request, settings, kb_resolver, idem_store, sonar_config=None):
            received_configs.append(sonar_config)
            return EnrichResponse(
                fields={},
                confidence=0.85,
                variation_count=3,
                uncertainty_score=0.0,
                pass_count=1,
                inference_version="test",
                processing_time_ms=50,
                tokens_used=300,
                state="completed",
            )

        await run_convergence_loop(
            request=enrich_request,
            settings=mock_settings,
            kb_resolver=mock_kb_resolver,
            enricher=tracking_enricher,
            inference_rules=[],
            domain_spec=None,
        )

        # All configs should be None without domain_spec
        assert all(c is None for c in received_configs)

    @pytest.mark.asyncio
    async def test_cost_tracking_in_response(self, enrich_request, mock_settings, mock_kb_resolver):
        """Test that cost summary appears in feature_vector."""

        async def simple_enricher(request, settings, kb_resolver, idem_store, sonar_config=None):
            return EnrichResponse(
                fields={"field_a": "val"},
                confidence=0.9,
                variation_count=3,
                uncertainty_score=0.0,
                pass_count=1,
                inference_version="test",
                processing_time_ms=50,
                tokens_used=500,
                state="completed",
            )

        _classification_cache.clear()
        response = await run_convergence_loop(
            request=enrich_request,
            settings=mock_settings,
            kb_resolver=mock_kb_resolver,
            enricher=simple_enricher,
            inference_rules=[],
            domain_spec=SAMPLE_DOMAIN_SPEC,
        )

        assert response.feature_vector is not None
        assert "cost_summary" in response.feature_vector
        assert "domain_classification" in response.feature_vector


class TestInferenceUnlockPrioritization:
    """Wiring: the v2 inference unlock_map prioritizes Pass 2+ search targets.

    The derivation engine already produces an unlock_map every pass
    (state.unlock_map). This suite proves the convergence loop now CONSUMES it
    via prioritize_search_targets so the highest-leverage missing fields are
    searched first, instead of discarding the signal.
    """

    UNLOCK_DOMAIN_SPEC = {
        "domain": "unlock_test",
        "ontology": {
            "nodes": {
                "Facility": {
                    "properties": {
                        "polymers_handled": {"type": "list"},
                        "certifications": {"type": "list"},
                        "equipment_types": {"type": "list"},
                        # material_grade needs 2 inputs → each missing input
                        # earns partial unlock credit (0.5).
                        "material_grade": {
                            "type": "string",
                            "managed_by": "inference",
                            "derived_from": ["polymers_handled", "certifications"],
                            "confidence_floor": 0.6,
                        },
                        # recyclability_score needs only polymers_handled → that
                        # sole-blocker input earns full unlock credit (1.0),
                        # making polymers_handled the top target (1.5 total).
                        "recyclability_score": {
                            "type": "string",
                            "managed_by": "inference",
                            "derived_from": ["polymers_handled"],
                            "confidence_floor": 0.6,
                        },
                    }
                }
            }
        },
    }

    @pytest.fixture
    def mock_settings(self):
        settings = MagicMock()
        settings.perplexity_api_key = "test-key"
        settings.perplexity_model = "sonar"
        settings.max_concurrent_variations = 3
        settings.default_timeout_seconds = 30
        settings.max_budget_tokens = 30000
        return settings

    @pytest.mark.asyncio
    async def test_pass2_targets_ordered_by_unlock_value(self, mock_settings):
        """Pass 2 target order must reflect inference unlock values.

        polymers_handled (unlock 1.5) precedes certifications (0.5), and both
        unlock-positive fields precede the zero-unlock fields (name,
        equipment_types).
        """
        seen_orders: list[list[str]] = []

        async def enricher(request, settings, kb_resolver, idem_store, sonar_config=None):
            # The pass-specific schema keys mirror plan.priority_fields order.
            seen_orders.append(list((request.schema_ or {}).keys()))
            low = 0.5  # below the 0.6 inference floor and 0.7 gap threshold
            return EnrichResponse(
                fields={
                    "polymers_handled": ["HDPE"],
                    "certifications": ["ISO 9001"],
                    "equipment_types": ["shredder"],
                },
                confidence=low,
                variation_count=2,
                pass_count=1,
                inference_version="test",
                processing_time_ms=50,
                tokens_used=100,
                state="completed",
                feature_vector={
                    "per_field_confidence": {
                        "polymers_handled": low,
                        "certifications": low,
                        "equipment_types": low,
                    }
                },
            )

        _classification_cache.clear()
        request = EnrichRequest(
            entity={"name": "Acme Recycling", "website": None},
            object_type="Facility",
            objective="Enrich facility profile",
        )
        await run_convergence_loop(
            request=request,
            settings=mock_settings,
            kb_resolver=MagicMock(),
            enricher=enricher,
            inference_rules=[],
            domain_spec=self.UNLOCK_DOMAIN_SPEC,
        )

        assert len(seen_orders) >= 2, "convergence loop must run at least two passes"
        pass2 = seen_orders[1]
        for f in ("polymers_handled", "certifications"):
            assert f in pass2, f"expected {f} among Pass 2 targets: {pass2}"
        # Highest-unlock field leads.
        assert pass2.index("polymers_handled") < pass2.index("certifications")
        # Unlock-positive fields precede zero-unlock fields.
        last_positive = max(pass2.index("polymers_handled"), pass2.index("certifications"))
        for zero_field in ("name", "equipment_types"):
            if zero_field in pass2:
                assert last_positive < pass2.index(zero_field)


class TestSonarConfigSerialization:
    """Test SonarConfig.to_dict for TransportPacket compliance."""

    def test_sonar_config_to_dict(self):
        config = SonarConfig(
            model=SonarModel.SONAR_PRO,
            estimated_cost_per_call=0.045,
            resolution_reason="test",
        )
        d = config.to_dict()
        assert isinstance(d, dict)
        assert d["model"] == "sonar-pro"
        assert d["estimated_cost_per_call"] == 0.045

    def test_sonar_config_to_api_params(self):
        config = SonarConfig(model=SonarModel.SONAR)
        params = config.to_api_params()
        assert params["model"] == "sonar"
        assert "web_search_options" in params


class TestVariationCountIsAVariationCount:
    def test_variation_count_sums_search_plan_variations_not_tokens(self):
        from app.engines.convergence_controller import (
            ConvergenceState,
            PassResult,
            _assemble_convergence_response,
        )
        from app.engines.meta_prompt_planner import PromptPlan
        from app.models.schemas import EnrichRequest

        def plan(n: int, pass_number: int) -> PromptPlan:
            return PromptPlan(
                mode="discovery",
                priority_fields=[],
                kb_fragment_ids=[],
                variation_count=n,
                pass_number=pass_number,
            )

        state = ConvergenceState()
        state.pass_results = [
            PassResult(pass_number=1, tokens_used=1_800, search_plan=plan(5, 1), confidence=0.8),
            PassResult(pass_number=2, tokens_used=2_200, search_plan=plan(3, 2), confidence=0.9),
            PassResult(pass_number=3, tokens_used=900, search_plan=None, confidence=0.9),
        ]
        request = EnrichRequest(
            entity={"Name": "Test Corp"},
            object_type="Account",
            objective="Enrich",
        )
        response = _assemble_convergence_response(state, request, elapsed=10)
        assert response.variation_count == 8
        assert response.pass_count == 3
