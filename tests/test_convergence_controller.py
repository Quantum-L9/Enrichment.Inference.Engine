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
    async def test_convergence_flow_accepts_raw_domain_spec(
        self, mock_settings, mock_kb_resolver
    ) -> None:
        seen_requests: list[EnrichRequest] = []

        async def enricher(request, settings, kb_resolver, idem_store, sonar_config=None):
            seen_requests.append(request)
            return EnrichResponse(
                fields={"facility_type": "MRF"},
                confidence=0.95,
                variation_count=1,
                pass_count=1,
                inference_version="test",
                processing_time_ms=10,
                tokens_used=100,
                state="completed",
                feature_vector={"per_field_confidence": {"facility_type": 0.95}},
            )

        request = EnrichRequest.model_validate(
            {
                "entity": {"name": "Acme Recycling"},
                "object_type": "Facility",
                "schema": {"facility_type": "string"},
                "objective": "Find the facility type.",
                "max_variations": 1,
            }
        )
        raw_domain_spec = {
            "domain": "test_domain",
            "ontology": {
                "nodes": {
                    "Facility": {
                        "properties": {
                            "facility_type": {
                                "metadata": {"gate_critical": True, "scoring_weight": 0.75}
                            }
                        }
                    }
                }
            },
        }

        response = await run_convergence_loop(
            request=request,
            settings=mock_settings,
            kb_resolver=mock_kb_resolver,
            enricher=enricher,
            inference_rules=[],
            domain_spec=raw_domain_spec,
        )

        assert response.fields == {"facility_type": "MRF"}
        assert len(seen_requests) == 1
        assert seen_requests[0].schema_ == {"facility_type": "string"}

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
    async def test_first_pass_stops_when_requested_fields_clear_their_bars(
        self, mock_settings, mock_kb_resolver
    ):
        calls = 0

        async def enricher(request, settings, kb_resolver, idem_store, sonar_config=None):
            nonlocal calls
            calls += 1
            return EnrichResponse(
                fields={"state": "North Carolina", "website": "https://example.com"},
                confidence=1.0,
                variation_count=1,
                uncertainty_score=0.0,
                pass_count=1,
                inference_version="test",
                processing_time_ms=10,
                tokens_used=100,
                state="completed",
                feature_vector={
                    "per_field_confidence": {"state": 0.99, "website": 0.99},
                },
            )

        request = EnrichRequest(
            entity={"name": "Scrap Management Inc"},
            object_type="plasticos",
            objective="state and website",
            schema={"state": "string", "website": "string"},
            field_thresholds={"state": 0.95, "website": 0.99},
            max_variations=1,
        )
        response = await run_convergence_loop(
            request=request,
            settings=mock_settings,
            kb_resolver=mock_kb_resolver,
            enricher=enricher,
            inference_rules=[],
        )
        assert calls == 1
        assert response.pass_count == 1
        assert response.fields["state"] == "North Carolina"
        assert response.fields["website"] == "https://example.com"

    @pytest.mark.asyncio
    async def test_first_pass_continues_when_a_requested_field_is_missing(
        self, mock_settings, mock_kb_resolver
    ):
        calls = 0

        async def enricher(request, settings, kb_resolver, idem_store, sonar_config=None):
            nonlocal calls
            calls += 1
            fields = (
                {"state": "North Carolina"}
                if calls == 1
                else {
                    "state": "North Carolina",
                    "website": "https://example.com",
                }
            )
            return EnrichResponse(
                fields=fields,
                confidence=1.0,
                variation_count=1,
                uncertainty_score=0.0,
                pass_count=1,
                inference_version="test",
                processing_time_ms=10,
                tokens_used=100,
                state="completed",
                feature_vector={"per_field_confidence": dict.fromkeys(fields, 0.99)},
            )

        request = EnrichRequest(
            entity={"name": "Scrap Management Inc"},
            object_type="plasticos",
            objective="state and website",
            schema={"state": "string", "website": "string"},
            field_thresholds={"state": 0.95, "website": 0.99},
            max_variations=1,
        )
        response = await run_convergence_loop(
            request=request,
            settings=mock_settings,
            kb_resolver=mock_kb_resolver,
            enricher=enricher,
            inference_rules=[],
        )
        assert calls == 2
        assert response.pass_count == 2
        assert "website" in response.fields

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


def test_caller_schema_is_the_pass_target() -> None:
    """Odoo's writeback schema must survive pass 1 discovery.

    Discovery otherwise replaces the schema with entity keys that merely
    have low confidence, so a request for `street` never reaches the provider
    and the Odoo backfill comes back empty.
    """
    from app.engines.convergence_controller import _build_pass_request
    from app.engines.meta_prompt_planner import PromptPlan

    plan = PromptPlan(
        mode="discovery",
        priority_fields=["name", "website", "city", "zip", "email", "phone", "id"],
        kb_fragment_ids=[],
        variation_count=2,
        pass_number=1,
    )
    request = EnrichRequest.model_validate(
        {
            "entity": {"name": "Scrap Management Inc", "city": "Charlotte"},
            "object_type": "plasticos",
            "schema": {"street": "string"},
            "objective": "Return only the street address line.",
            "max_variations": 1,
        }
    )
    built = _build_pass_request(request, plan, ConvergenceState(), pass_num=1)
    assert built.schema_ == {"street": "string"}
    assert built.max_variations == 1


@pytest.mark.asyncio
async def test_high_confidence_reply_skips_later_variations() -> None:
    from app.engines.enrichment_orchestrator import _collect_variations
    from app.services.perplexity_client import SonarResponse

    calls = 0

    async def call() -> SonarResponse:
        nonlocal calls
        calls += 1
        return SonarResponse(
            data={"confidence": 0.98, "fields": {"street": "10612-D Providence Rd., #750"}},
            tokens_used=10,
        )

    results = await _collect_variations(
        call=call,
        variation_count=2,
        target_schema={"street": "string"},
        threshold=0.65,
    )
    assert calls == 1
    assert len(results) == 1


@pytest.mark.asyncio
async def test_low_confidence_reply_runs_the_remaining_budget() -> None:
    from app.engines.enrichment_orchestrator import _collect_variations
    from app.services.perplexity_client import SonarResponse

    calls = 0

    async def call() -> SonarResponse:
        nonlocal calls
        calls += 1
        return SonarResponse(
            data={"confidence": 0.2, "fields": {"street": "somewhere"}},
            tokens_used=10,
        )

    results = await _collect_variations(
        call=call,
        variation_count=2,
        target_schema={"street": "string"},
        threshold=0.65,
    )
    assert calls == 2
    assert len(results) == 2


@pytest.mark.asyncio
async def test_schema_less_empty_fields_do_not_short_circuit() -> None:
    from app.engines.enrichment_orchestrator import _collect_variations
    from app.services.perplexity_client import SonarResponse

    calls = 0

    async def call() -> SonarResponse:
        nonlocal calls
        calls += 1
        return SonarResponse(data={"confidence": 0.9, "fields": {}}, tokens_used=1)

    results = await _collect_variations(
        call=call,
        variation_count=2,
        target_schema=None,
        threshold=0.65,
    )
    assert calls == 2
    assert len(results) == 2


@pytest.mark.asyncio
async def test_schema_less_reply_with_a_field_stops() -> None:
    from app.engines.enrichment_orchestrator import _collect_variations
    from app.services.perplexity_client import SonarResponse

    calls = 0

    async def call() -> SonarResponse:
        nonlocal calls
        calls += 1
        return SonarResponse(
            data={"confidence": 0.9, "fields": {"street": "1 Main"}},
            tokens_used=1,
        )

    results = await _collect_variations(
        call=call,
        variation_count=2,
        target_schema=None,
        threshold=0.65,
    )
    assert calls == 1
    assert len(results) == 1


@pytest.mark.asyncio
async def test_remaining_variations_overlap() -> None:
    import asyncio

    from app.engines.enrichment_orchestrator import _collect_variations
    from app.services.perplexity_client import SonarResponse

    calls = 0
    in_flight = 0
    peak = 0

    async def call() -> SonarResponse:
        nonlocal calls, in_flight, peak
        calls += 1
        if calls == 1:
            return SonarResponse(
                data={"confidence": 0.1, "fields": {"street": "x"}},
                tokens_used=1,
            )
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return SonarResponse(
            data={"confidence": 0.1, "fields": {"street": "y"}},
            tokens_used=1,
        )

    results = await _collect_variations(
        call=call,
        variation_count=3,
        target_schema={"street": "string"},
        threshold=0.65,
    )
    assert calls == 3
    assert peak == 2
    assert len(results) == 3


def test_field_thresholds_reject_non_finite() -> None:
    from pydantic import ValidationError

    from app.models.schemas import EnrichRequest

    with pytest.raises(ValidationError, match="finite"):
        EnrichRequest(
            entity={"name": "Acme"},
            object_type="Account",
            objective="fill street",
            schema={"street": "string"},
            field_thresholds={"street": "NaN"},
        )


# ── WU-03 / F-003: a converge whose passes all failed reports failed ──────


def _settings(max_budget_tokens: int = 30000) -> MagicMock:
    settings = MagicMock()
    settings.perplexity_api_key = "test-key"
    settings.perplexity_model = "sonar"
    settings.max_concurrent_variations = 3
    settings.default_timeout_seconds = 30
    settings.max_budget_tokens = max_budget_tokens
    return settings


def _scripted_enricher(responses: list[EnrichResponse], seen: list | None = None):
    """An enricher that returns the scripted responses in order (last one repeats)."""
    calls = {"n": 0}

    async def enricher(pass_request, *args, **kwargs):
        if seen is not None:
            seen.append(pass_request)
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[i]

    return enricher


def _failed_pass(reason: str = "no_valid_responses") -> EnrichResponse:
    return EnrichResponse(state="failed", failure_reason=reason, fields={}, tokens_used=50)


def _pass(confidence: float, fields: dict | None = None, tokens: int = 100) -> EnrichResponse:
    fields = fields if fields is not None else {"industry": f"v{confidence}"}
    return EnrichResponse(
        state="completed",
        fields=fields,
        confidence=confidence,
        tokens_used=tokens,
        feature_vector={"per_field_confidence": dict.fromkeys(fields, confidence)},
    )


class TestPassFailurePropagation:
    @pytest.mark.asyncio
    async def test_all_passes_failed_reports_failed_with_reason(self) -> None:
        from app.engines.convergence.convergence_config import ConvergenceConfig

        response = await run_convergence_loop(
            request=EnrichRequest(entity={"name": "Acme"}, object_type="Account", objective="x"),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_failed_pass("no_valid_responses: timeout")]),
            convergence_config=ConvergenceConfig(max_passes=3),
        )
        assert response.state == "failed"
        assert response.failure_reason
        assert "no_valid_responses: timeout" in response.failure_reason
        assert response.pass_count >= 1

    @pytest.mark.asyncio
    async def test_a_later_successful_pass_still_completes(self) -> None:
        from app.engines.convergence.convergence_config import ConvergenceConfig

        response = await run_convergence_loop(
            request=EnrichRequest(
                entity={"name": "Acme"},
                object_type="Account",
                objective="x",
                schema={"industry": "string"},
            ),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_failed_pass(), _pass(0.95)]),
            convergence_config=ConvergenceConfig(max_passes=3),
        )
        assert response.state == "completed"
        assert response.failure_reason is None


# ── WU-02 / F-002 / F-058 / D-9 v2: per-pass uncertainty ──────────────────


class TestPerPassUncertainty:
    @pytest.mark.asyncio
    async def test_rising_confidence_runs_four_passes_and_stops_on_threshold(self) -> None:
        from app.engines.convergence.convergence_config import ConvergenceConfig

        response = await run_convergence_loop(
            request=EnrichRequest(entity={"name": "Acme"}, object_type="Account", objective="x"),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_pass(0.3), _pass(0.5), _pass(0.7), _pass(0.9)]),
            convergence_config=ConvergenceConfig(max_passes=5),
        )

        assert response.state == "completed"
        assert response.pass_count == 4
        modes = [entry["mode"] for entry in response.inferences]
        assert modes == ["discovery", "targeted", "targeted", "targeted"]
        uncertainties = [entry["uncertainty"] for entry in response.inferences]
        assert uncertainties == pytest.approx([7.0, 5.0, 3.0, 1.0])
        assert response.uncertainty_score == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_reported_uncertainty_tracks_the_last_pass_not_a_constant(self) -> None:
        from app.engines.convergence.convergence_config import ConvergenceConfig

        response = await run_convergence_loop(
            request=EnrichRequest(entity={"name": "Acme"}, object_type="Account", objective="x"),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_pass(0.3), _pass(0.5)]),
            convergence_config=ConvergenceConfig(max_passes=2),
        )
        assert response.uncertainty_score == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_variation_budget_follows_uncertainty_not_max_variations(self) -> None:
        from app.engines.convergence.convergence_config import ConvergenceConfig

        seen: list = []
        await run_convergence_loop(
            request=EnrichRequest(
                entity={"name": "Acme"}, object_type="Account", objective="x", max_variations=5
            ),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_pass(0.3), _pass(0.5), _pass(0.7), _pass(0.9)], seen),
            convergence_config=ConvergenceConfig(max_passes=5),
        )
        # Pass 2 follows U=7 (> 5.0 -> 5 variations); pass 3 follows U=5 (3).
        assert [r.max_variations for r in seen[1:3]] == [5, 3]

    @pytest.mark.asyncio
    async def test_discovery_pass_plans_with_maximal_uncertainty(self) -> None:
        """D-9 v2: nothing known yet is cold start (both scores 10), so pass 1 gets the largest budget.

        Pass 1 used to plan with a stale seed of U=1.0 and ran the *smallest*
        budget (observed [2, 5, 3, 3]) on the pass that knows the least.
        """
        from app.engines.convergence.confidence_tracker import UNCERTAINTY_SCALE
        from app.engines.convergence.convergence_config import ConvergenceConfig
        from app.engines.convergence_controller import ConvergenceState
        from app.engines.meta_prompt_planner import MetaPromptPlanner

        assert ConvergenceState().uncertainty_score == UNCERTAINTY_SCALE

        seen: list = []
        response = await run_convergence_loop(
            request=EnrichRequest(
                entity={"name": "Acme"}, object_type="Account", objective="x", max_variations=5
            ),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_pass(0.3), _pass(0.5), _pass(0.7), _pass(0.9)], seen),
            convergence_config=ConvergenceConfig(max_passes=5),
        )

        maximal_budget = MetaPromptPlanner()._compute_variation_budget(
            UNCERTAINTY_SCALE, budget_remaining=30000
        )
        assert maximal_budget == 5
        assert seen[0].max_variations == maximal_budget
        assert [r.max_variations for r in seen] == [5, 5, 3, 3]
        assert response.inferences[0]["mode"] == "discovery"


class TestUncertaintyFormulaD9:
    """D-9 v2 (operator decision 2026-11-11, issue #266), superseding D-9 v1.

    U_mean = 10 * (1 - mean c*) stays the diagnostic score; U_action =
    10 * (1 - min c*) drives planning. Both use one effective-confidence
    population. D-9 v1's "owner-accepted 2026-10-29" claim was never verified;
    v2 is a new decision, not a confirmation of v1.
    """

    def test_confidence_ladder_maps_to_seven_five_three_one(self) -> None:
        """T27: with one field, both scores give the familiar ladder."""
        from app.engines.convergence.confidence_tracker import (
            action_uncertainty_from_confidences,
            uncertainty_from_confidences,
        )

        ladder = (0.3, 0.5, 0.7, 0.9)
        assert [uncertainty_from_confidences([c]) for c in ladder] == pytest.approx(
            [7.0, 5.0, 3.0, 1.0]
        )
        assert [action_uncertainty_from_confidences([c]) for c in ladder] == pytest.approx(
            [7.0, 5.0, 3.0, 1.0]
        )

    def test_distinct_confidence_states_give_distinct_u(self) -> None:
        from app.engines.convergence.confidence_tracker import uncertainty_from_confidences

        assert uncertainty_from_confidences([0.4, 0.6]) != uncertainty_from_confidences([0.9, 0.9])

    def test_mean_and_action_diverge_on_a_weak_field(self) -> None:
        """T01: [0.95, 0.30, 0.80] -> U_mean 3.1667, U_action 7."""
        from app.engines.convergence.confidence_tracker import (
            action_uncertainty_from_confidences,
            uncertainty_from_confidences,
        )

        confidences = [0.95, 0.30, 0.80]
        assert uncertainty_from_confidences(confidences) == pytest.approx(3.166667, abs=1e-6)
        assert action_uncertainty_from_confidences(confidences) == pytest.approx(7.0)

    def test_one_missing_field_cannot_be_diluted(self) -> None:
        """T02: nine fields at 1.0 and one absent -> U_mean 1, U_action 10."""
        from app.engines.convergence.confidence_tracker import ConfidenceTracker

        names = [f"f{i}" for i in range(10)]
        tracker = ConfidenceTracker(required_fields=names)
        for name in names[:9]:
            tracker.update_field(name, "v", 1.0, 1, "consensus")
        assert tracker.uncertainty(names) == pytest.approx(1.0)
        assert tracker.action_uncertainty(names) == pytest.approx(10.0)

    def test_missing_requested_field_counts_as_unknown(self) -> None:
        from app.engines.convergence.confidence_tracker import ConfidenceTracker

        tracker = ConfidenceTracker()
        tracker.update_field("a", "x", 0.8, 1, "consensus")
        assert tracker.uncertainty(["a", "b"]) == pytest.approx(6.0)
        assert tracker.action_uncertainty(["a", "b"]) == pytest.approx(10.0)
        assert tracker.uncertainty(["a"]) == pytest.approx(2.0)
        assert tracker.action_uncertainty(["a"]) == pytest.approx(2.0)

    def test_nothing_known_is_maximal_uncertainty(self) -> None:
        """T14: an empty population is cold start for both scores, and not converged."""
        from app.engines.convergence.confidence_tracker import (
            UNCERTAINTY_SCALE,
            ConfidenceTracker,
        )

        tracker = ConfidenceTracker()
        assert tracker.uncertainty() == UNCERTAINTY_SCALE
        assert tracker.action_uncertainty() == UNCERTAINTY_SCALE
        assert tracker.has_converged() is False

    @pytest.mark.parametrize(
        "raw", [None, float("nan"), float("inf"), float("-inf"), "high", True, False, object()]
    )
    def test_unusable_confidence_is_zero_and_scores_stay_finite(self, raw: object) -> None:
        """T08: absent, non-numeric, non-finite and boolean confidence count as 0."""
        import math

        from app.engines.convergence.confidence_tracker import (
            action_uncertainty_from_confidences,
            normalize_confidence,
            uncertainty_from_confidences,
        )

        assert normalize_confidence(raw) == 0.0
        for score in (
            uncertainty_from_confidences([raw, 0.9]),
            action_uncertainty_from_confidences([raw, 0.9]),
        ):
            assert math.isfinite(score)
            assert 0.0 <= score <= 10.0
        assert action_uncertainty_from_confidences([raw, 0.9]) == pytest.approx(10.0)

    @pytest.mark.parametrize(
        ("raw", "expected"), [(1.5, 1.0), (-0.2, 0.0), ("0.7", 0.7), (0.42, 0.42), (1, 1.0)]
    )
    def test_finite_confidence_is_clamped(self, raw: object, expected: float) -> None:
        """T09: out-of-range values clamp to [0, 1]; finite numeric strings still count."""
        from app.engines.convergence.confidence_tracker import normalize_confidence

        assert normalize_confidence(raw) == pytest.approx(expected)

    def test_missing_value_with_high_confidence_is_not_evidence(self) -> None:
        """T10: a None or empty value at confidence 0.99 is effective confidence 0."""
        from app.engines.convergence.confidence_tracker import ConfidenceTracker

        for empty in (None, ""):
            tracker = ConfidenceTracker(required_fields=["street"])
            tracker.update_field("street", empty, 0.99, 1, "consensus")
            assert tracker.effective_confidence("street") == 0.0
            assert tracker.field_ready("street") is False
            assert tracker.has_converged() is False
            assert tracker.action_uncertainty(["street"]) == pytest.approx(10.0)

    def test_zero_and_false_are_values(self) -> None:
        """T11: legitimate falsy values are present and can be ready."""
        from app.engines.convergence.confidence_tracker import ConfidenceTracker

        tracker = ConfidenceTracker(required_fields=["employees", "is_public"])
        tracker.update_field("employees", 0, 0.9, 1, "consensus")
        tracker.update_field("is_public", False, 0.9, 1, "consensus")
        assert tracker.field_ready("employees") is True
        assert tracker.field_ready("is_public") is True
        assert tracker.has_converged() is True

    def test_consumer_threshold_governs_readiness_not_the_aggregate(self) -> None:
        """T04: confidence 0.90 against a 0.95 consumer bar: U_action 1, still not ready."""
        from app.engines.convergence.confidence_tracker import ConfidenceTracker

        tracker = ConfidenceTracker(
            confidence_threshold=0.85,
            required_fields=["street"],
            field_thresholds={"street": 0.95},
        )
        tracker.update_field("street", "1 Main St", 0.90, 1, "consensus")
        assert tracker.action_uncertainty(["street"]) == pytest.approx(1.0)
        assert tracker.field_ready("street") is False
        assert tracker.has_converged() is False
        summary = tracker.get_pass_summary()
        assert summary["converged"] is False
        assert summary["fields_below_threshold"] == 1

    def test_schema_free_ignores_inference_only_fields(self) -> None:
        """T15: without a schema, inference-derived fields never decide convergence."""
        from app.engines.convergence.confidence_tracker import ConfidenceTracker

        tracker = ConfidenceTracker(exclude_sources=frozenset({"inference"}))
        tracker.update_field("material_grade", "A", 0.99, 1, "inference")
        assert tracker.deciding_fields() == []
        assert tracker.has_converged() is False
        assert tracker.action_uncertainty() == pytest.approx(10.0)

    def test_explicit_schema_counts_a_required_inferred_field(self) -> None:
        """T16: a requested field decides readiness whatever source set it (unchanged policy)."""
        from app.engines.convergence.confidence_tracker import ConfidenceTracker

        tracker = ConfidenceTracker(
            exclude_sources=frozenset({"inference"}), required_fields=["material_grade"]
        )
        tracker.update_field("material_grade", "A", 0.9, 1, "inference")
        assert tracker.deciding_fields() == ["material_grade"]
        assert tracker.has_converged() is True


class TestTrackerStopSignals:
    """F-058: summary agrees with converged; improvement is measured on this pass."""

    def test_summary_agrees_with_converged(self) -> None:
        from app.engines.convergence.confidence_tracker import ConfidenceTracker

        tracker = ConfidenceTracker(exclude_sources=frozenset({"inference"}))
        tracker.update_field("a", "x", 0.9, 1, "consensus")
        tracker.update_field("derived", "y", 0.5, 1, "inference")
        summary = tracker.get_pass_summary()
        assert summary["converged"] is True
        assert summary["fields_below_threshold"] == 0
        assert summary["fields_above_threshold"] == 1

    def test_an_earlier_pass_gain_is_not_credited_again(self) -> None:
        from app.engines.convergence.confidence_tracker import ConfidenceTracker

        tracker = ConfidenceTracker()
        tracker.update_field("a", "x", 0.3, 1, "consensus")
        tracker.update_field("a", "x", 0.6, 2, "consensus")
        tracker.update_field("b", "y", 0.5, 2, "consensus")
        tracker.update_field("b", "y", 0.51, 3, "consensus")
        # Pass 3 moved only b, by 0.01; a's +0.3 belongs to pass 2.
        assert tracker.had_meaningful_improvement(0.05) is False

    def test_a_field_first_resolved_this_pass_is_an_improvement(self) -> None:
        from app.engines.convergence.confidence_tracker import ConfidenceTracker

        tracker = ConfidenceTracker()
        tracker.update_field("a", "x", 0.6, 1, "consensus")
        tracker.update_field("b", "y", 0.5, 2, "consensus")
        assert tracker.had_meaningful_improvement(0.05) is True


# ── WU-04 / F-016 F-017 F-019: pass schemas built by the loop ─────────────


class TestPassTargeting:
    @pytest.mark.asyncio
    async def test_pass_one_schema_targets_null_entity_fields(self) -> None:
        from app.engines.convergence.convergence_config import ConvergenceConfig

        seen: list = []
        await run_convergence_loop(
            request=EnrichRequest(
                entity={"name": "Acme", "industry": None, "employees": None},
                object_type="Account",
                objective="x",
            ),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_pass(0.95, {"industry": "x", "employees": 10})], seen),
            convergence_config=ConvergenceConfig(max_passes=1),
        )
        assert {"industry", "employees"} <= set(seen[0].schema_ or {})

    @pytest.mark.asyncio
    async def test_field_at_point_eight_is_in_the_pass_two_schema(self) -> None:
        from app.engines.convergence.convergence_config import ConvergenceConfig

        seen: list = []
        await run_convergence_loop(
            request=EnrichRequest(entity={"name": "Acme"}, object_type="Account", objective="x"),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_pass(0.8), _pass(0.9)], seen),
            convergence_config=ConvergenceConfig(max_passes=3, confidence_threshold=0.85),
        )
        assert len(seen) >= 2
        assert "industry" in (seen[1].schema_ or {})

    @pytest.mark.asyncio
    async def test_domain_priority_fields_lead_the_pass_one_schema(self) -> None:
        from app.engines.convergence.convergence_config import ConvergenceConfig

        seen: list = []
        await run_convergence_loop(
            request=EnrichRequest(
                entity={"name": "Acme", "industry": None}, object_type="Account", objective="x"
            ),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_pass(0.95, {"annual_tonnage": 5})], seen),
            domain_hints={"priority_fields": ["annual_tonnage", "website"]},
            convergence_config=ConvergenceConfig(max_passes=1),
        )
        assert list(seen[0].schema_ or {})[:2] == ["annual_tonnage", "website"]


# ── WU-05 / F-018: the response names why the loop stopped ────────────────


class TestConvergenceReason:
    @pytest.mark.asyncio
    async def test_budget_exhaustion_is_reported(self) -> None:
        from app.engines.convergence.convergence_config import ConvergenceConfig

        response = await run_convergence_loop(
            request=EnrichRequest(entity={"name": "Acme"}, object_type="Account", objective="x"),
            settings=_settings(max_budget_tokens=150),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_pass(0.3, tokens=200), _pass(0.5, tokens=200)]),
            convergence_config=ConvergenceConfig(max_passes=5),
        )
        assert "budget" in (response.convergence_reason or "")

    @pytest.mark.asyncio
    async def test_insufficient_improvement_is_reported(self) -> None:
        from app.engines.convergence.convergence_config import ConvergenceConfig

        response = await run_convergence_loop(
            request=EnrichRequest(entity={"name": "Acme"}, object_type="Account", objective="x"),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_pass(0.3), _pass(0.31)]),
            convergence_config=ConvergenceConfig(max_passes=5),
        )
        assert response.convergence_reason == "insufficient_improvement_delta_0.05"

    @pytest.mark.asyncio
    async def test_running_out_of_passes_is_reported(self) -> None:
        from app.engines.convergence.convergence_config import ConvergenceConfig

        response = await run_convergence_loop(
            request=EnrichRequest(entity={"name": "Acme"}, object_type="Account", objective="x"),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_pass(0.3), _pass(0.5)]),
            convergence_config=ConvergenceConfig(max_passes=2),
        )
        assert response.convergence_reason == "max_passes_reached"

    @pytest.mark.asyncio
    async def test_threshold_stop_is_reported(self) -> None:
        response = await run_convergence_loop(
            request=EnrichRequest(entity={"name": "Acme"}, object_type="Account", objective="x"),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_pass(0.95)]),
        )
        assert response.convergence_reason == "all_fields_above_threshold_0.85"


# ── WU-06 / F-007: a per-run budget below settings caps spend ─────────────


@pytest.mark.asyncio
async def test_request_budget_below_settings_caps_spend() -> None:
    from app.engines.convergence.convergence_config import ConvergenceConfig

    seen: list = []
    response = await run_convergence_loop(
        request=EnrichRequest(entity={"name": "Acme"}, object_type="Account", objective="x"),
        settings=_settings(max_budget_tokens=30000),
        kb_resolver=MagicMock(),
        enricher=_scripted_enricher([_pass(0.3, tokens=200), _pass(0.5, tokens=200)], seen),
        convergence_config=ConvergenceConfig(max_passes=5, max_budget_tokens=150),
    )
    assert len(seen) == 1
    assert response.convergence_reason == "budget_exhausted"


@pytest.mark.asyncio
async def test_request_budget_never_raises_the_settings_budget() -> None:
    from app.engines.convergence.convergence_config import ConvergenceConfig

    seen: list = []
    await run_convergence_loop(
        request=EnrichRequest(entity={"name": "Acme"}, object_type="Account", objective="x"),
        settings=_settings(max_budget_tokens=150),
        kb_resolver=MagicMock(),
        enricher=_scripted_enricher([_pass(0.3, tokens=200), _pass(0.5, tokens=200)], seen),
        convergence_config=ConvergenceConfig(max_passes=5, max_budget_tokens=1_000_000),
    )
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_failed_passes_do_not_reuse_an_earlier_passes_gain() -> None:
    """A pass that records nothing must not be credited with the previous
    pass's improvement; the loop stops instead of paying for every remaining pass."""
    from app.engines.convergence.convergence_config import ConvergenceConfig

    seen: list = []
    response = await run_convergence_loop(
        request=EnrichRequest(entity={"name": "Acme"}, object_type="Account", objective="x"),
        settings=_settings(),
        kb_resolver=MagicMock(),
        enricher=_scripted_enricher([_pass(0.5, {"a": 1, "b": 2}), _failed_pass()], seen),
        convergence_config=ConvergenceConfig(max_passes=6),
    )
    assert len(seen) == 2
    assert response.convergence_reason == "insufficient_improvement_delta_0.05"


def test_begin_pass_advances_current_pass_without_fields() -> None:
    from app.engines.convergence.confidence_tracker import ConfidenceTracker

    tracker = ConfidenceTracker()
    tracker.update_field("a", "x", 0.5, 1, "consensus")
    tracker.begin_pass(2)
    assert tracker.had_meaningful_improvement(0.05) is False


# ── D-9 v2: dual-score wiring through the real loop (issue #266) ───────────


def _fields_pass(confidences: dict[str, float], tokens: int = 100) -> EnrichResponse:
    """A completed pass whose fields carry their own per-field confidence."""
    fields = {name: f"v-{name}" for name in confidences}
    return EnrichResponse(
        state="completed",
        fields=fields,
        confidence=sum(confidences.values()) / max(len(confidences), 1),
        tokens_used=tokens,
        feature_vector={"per_field_confidence": dict(confidences)},
    )


def _tracking(response: EnrichResponse) -> dict:
    assert response.feature_vector is not None
    return response.feature_vector["confidence_tracking"]


class TestD9V2Loop:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("schema", [None, {}])
    async def test_schema_free_requests_keep_discovery(self, schema) -> None:
        """T12/T13: an omitted or empty schema keeps schema-free discovery on pass 1."""
        from app.engines.convergence.convergence_config import ConvergenceConfig
        from app.engines.convergence_controller import _required_fields

        request = EnrichRequest(
            entity={"name": "Acme", "industry": None},
            object_type="Account",
            objective="x",
            schema=schema,
        )
        assert _required_fields(request) is None
        seen: list = []
        response = await run_convergence_loop(
            request=request,
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_pass(0.95, {"industry": "Recycling"})], seen),
            convergence_config=ConvergenceConfig(max_passes=2),
        )
        assert response.inferences[0]["mode"] == "discovery"
        assert "industry" in (seen[0].schema_ or {})

    def test_reserved_confidence_key_is_not_a_required_field(self) -> None:
        from app.engines.convergence_controller import _required_fields

        request = EnrichRequest(
            entity={"name": "Acme"},
            object_type="Account",
            objective="x",
            schema={"industry": "string", "confidence": "float"},
        )
        assert _required_fields(request) == ["industry"]

    @pytest.mark.asyncio
    async def test_no_tracked_fields_is_cold_start_not_convergence(self) -> None:
        """T14: a pass that returns nothing leaves both scores at 10 and does not converge."""
        from app.engines.convergence.convergence_config import ConvergenceConfig

        response = await run_convergence_loop(
            request=EnrichRequest(entity={"name": "Acme"}, object_type="Account", objective="x"),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_pass(0.9, {})]),
            convergence_config=ConvergenceConfig(max_passes=2),
        )
        assert response.uncertainty_score == pytest.approx(10.0)
        assert _tracking(response)["converged"] is False
        assert not (response.convergence_reason or "").startswith("all_fields_above")

    @pytest.mark.asyncio
    async def test_failed_pass_updates_neither_score(self, monkeypatch) -> None:
        """T17: a failed pass carries both scores forward and learns nothing."""
        from app.engines.convergence.convergence_config import ConvergenceConfig

        seen_u: list[float] = []
        _spy_planner(monkeypatch, seen_u)
        response = await run_convergence_loop(
            request=EnrichRequest(
                entity={"name": "Acme"},
                object_type="Account",
                objective="x",
                schema={"industry": "string", "website": "string"},
            ),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher(
                [
                    _fields_pass({"industry": 0.9, "website": 0.3}),
                    _failed_pass(),
                    _fields_pass({"industry": 0.9, "website": 0.3}),
                ]
            ),
            convergence_config=ConvergenceConfig(max_passes=2),
        )
        # Pass 2 planned on pass 1's U_action (7); the failed pass changed nothing.
        assert seen_u == pytest.approx([10.0, 7.0])
        assert response.inferences[0]["uncertainty"] == pytest.approx(4.0)
        assert response.inferences[1]["uncertainty"] is None
        assert response.uncertainty_score == pytest.approx(4.0)
        assert response.failure_reason is None

    @pytest.mark.asyncio
    async def test_insufficient_improvement_is_not_convergence(self) -> None:
        """T18: a bounded stop without readiness keeps its reason and converged=False."""
        from app.engines.convergence.convergence_config import ConvergenceConfig

        response = await run_convergence_loop(
            request=EnrichRequest(entity={"name": "Acme"}, object_type="Account", objective="x"),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_pass(0.3), _pass(0.31)]),
            convergence_config=ConvergenceConfig(max_passes=5),
        )
        assert response.convergence_reason == "insufficient_improvement_delta_0.05"
        assert _tracking(response)["converged"] is False
        assert response.state == "completed"

    @pytest.mark.asyncio
    async def test_budget_exhaustion_is_not_convergence(self) -> None:
        """T19: budget exhaustion stops the loop without claiming readiness."""
        from app.engines.convergence.convergence_config import ConvergenceConfig

        response = await run_convergence_loop(
            request=EnrichRequest(entity={"name": "Acme"}, object_type="Account", objective="x"),
            settings=_settings(max_budget_tokens=150),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_pass(0.3, tokens=200)]),
            convergence_config=ConvergenceConfig(max_passes=5),
        )
        assert response.convergence_reason == "budget_exhausted"
        assert _tracking(response)["converged"] is False

    @pytest.mark.asyncio
    async def test_max_passes_is_not_threshold_success(self) -> None:
        """T20: running out of passes never reads as every field meeting its bar."""
        from app.engines.convergence.convergence_config import ConvergenceConfig

        response = await run_convergence_loop(
            request=EnrichRequest(entity={"name": "Acme"}, object_type="Account", objective="x"),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_pass(0.3), _pass(0.5)]),
            convergence_config=ConvergenceConfig(max_passes=2),
        )
        assert response.convergence_reason == "max_passes_reached"
        assert _tracking(response)["converged"] is False

    def test_both_scores_start_at_cold_start(self) -> None:
        from app.engines.convergence_controller import ConvergenceState

        state = ConvergenceState()
        assert state.converged is False
        assert state.uncertainty_score == pytest.approx(10.0)
        assert state.action_uncertainty == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_planner_receives_action_uncertainty(self, monkeypatch) -> None:
        """T28/T21: the real loop hands U_action to the planner and budgets 5 variations."""
        from app.engines.convergence.convergence_config import ConvergenceConfig

        seen_u: list[float] = []
        seen: list = []
        _spy_planner(monkeypatch, seen_u)
        response = await run_convergence_loop(
            request=EnrichRequest(
                entity={"name": "Acme"},
                object_type="Account",
                objective="x",
                schema={"industry": "string", "website": "string"},
                max_variations=5,
            ),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher(
                [
                    _fields_pass({"industry": 0.95, "website": 0.3}),
                    _fields_pass({"industry": 0.95, "website": 0.9}),
                ],
                seen,
            ),
            convergence_config=ConvergenceConfig(max_passes=3),
        )
        # Pass 1 is cold start; pass 2 plans on U_action = 10 * (1 - 0.3) = 7,
        # not on U_mean = 3.75, so it gets the 5-variation budget.
        assert seen_u[:2] == pytest.approx([10.0, 7.0])
        assert seen[1].max_variations == 5
        assert response.inferences[1]["mode"] == "targeted"

    @pytest.mark.asyncio
    async def test_planned_variations_never_exceed_the_request_cap(self) -> None:
        """T29: a high U_action is a plan, not permission to exceed max_variations."""
        from app.engines.convergence.convergence_config import ConvergenceConfig

        seen: list = []
        await run_convergence_loop(
            request=EnrichRequest(
                entity={"name": "Acme"},
                object_type="Account",
                objective="x",
                schema={"industry": "string", "website": "string"},
                max_variations=2,
            ),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher(
                [
                    _fields_pass({"industry": 0.95, "website": 0.3}),
                    _fields_pass({"industry": 0.95, "website": 0.9}),
                ],
                seen,
            ),
            convergence_config=ConvergenceConfig(max_passes=3),
        )
        assert all(r.max_variations <= 2 for r in seen)

    @pytest.mark.asyncio
    async def test_public_uncertainty_stays_the_diagnostic_mean(self) -> None:
        """T25/T26: uncertainty_score and inferences[*].uncertainty carry U_mean only."""
        from app.engines.convergence.convergence_config import ConvergenceConfig

        response = await run_convergence_loop(
            request=EnrichRequest(
                entity={"name": "Acme"},
                object_type="Account",
                objective="x",
                schema={"industry": "string", "website": "string"},
            ),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_fields_pass({"industry": 0.95, "website": 0.3})]),
            convergence_config=ConvergenceConfig(max_passes=1),
        )
        assert response.uncertainty_score == pytest.approx(3.75)
        assert response.inferences[0]["uncertainty"] == pytest.approx(3.75)
        assert "action_uncertainty" not in response.inferences[0]
        assert "action_uncertainty" not in response.model_dump()

    @pytest.mark.asyncio
    async def test_serialized_convergence_respects_consumer_thresholds(self) -> None:
        """T30/T04: 0.90 against a 0.95 consumer bar is not converged in the metadata."""
        from app.engines.convergence.convergence_config import ConvergenceConfig

        response = await run_convergence_loop(
            request=EnrichRequest(
                entity={"name": "Acme"},
                object_type="Account",
                objective="x",
                schema={"street": "string"},
                field_thresholds={"street": 0.95},
            ),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher(
                [_fields_pass({"street": 0.9}), _fields_pass({"street": 0.9})]
            ),
            convergence_config=ConvergenceConfig(max_passes=2),
        )
        tracking = _tracking(response)
        assert tracking["converged"] is False
        assert tracking["summary"]["fields_below_threshold"] == 1
        assert response.convergence_reason != "requested_fields_meet_field_thresholds"
        # U_action is 1 (< 2), yet the unmet consumer bar keeps pass 2 targeted.
        assert response.inferences[1]["mode"] == "targeted"

    @pytest.mark.asyncio
    async def test_consumer_threshold_met_converges(self) -> None:
        from app.engines.convergence.convergence_config import ConvergenceConfig

        response = await run_convergence_loop(
            request=EnrichRequest(
                entity={"name": "Acme"},
                object_type="Account",
                objective="x",
                schema={"street": "string"},
                field_thresholds={"street": 0.95},
            ),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_fields_pass({"street": 0.97})]),
            convergence_config=ConvergenceConfig(max_passes=2),
        )
        assert response.convergence_reason == "requested_fields_meet_field_thresholds"
        assert _tracking(response)["converged"] is True


def _spy_planner(monkeypatch: pytest.MonkeyPatch, seen_u: list[float]) -> None:
    """Record the U the real planner is given on each pass."""
    from app.engines.meta_prompt_planner import MetaPromptPlanner

    original = MetaPromptPlanner.plan_pass

    def spy(self, *args, **kwargs):
        seen_u.append(kwargs["uncertainty_score"])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MetaPromptPlanner, "plan_pass", spy)


# ── #320 review: the D-9 v2 seams where a plan becomes a provider call ───────


def _kb_resolver() -> MagicMock:
    resolver = MagicMock()
    resolver.resolve = MagicMock(
        return_value={"context_text": "", "content_hash": "h", "fragment_ids": [], "kb_files": []}
    )
    return resolver


def _real_provider_enricher(seen_configs: list):
    """The real enrich_entity, recording the SonarConfig the loop hands it."""
    from app.engines.enrichment_orchestrator import enrich_entity

    async def enricher(pass_request, settings, kb_resolver, idem_store, sonar_config=None):
        seen_configs.append(sonar_config)
        return await enrich_entity(
            pass_request, settings, kb_resolver, idem_store, sonar_config=sonar_config
        )

    return enricher


class TestReviewVariationCap:
    """F1: the optimizer's count never exceeds the pass budget or the caller's cap."""

    @pytest.mark.asyncio
    async def test_provider_calls_bounded_by_caller_max_with_domain_spec(self) -> None:
        from unittest.mock import AsyncMock, patch

        from app.engines.convergence.convergence_config import ConvergenceConfig
        from app.engines.search_optimizer import estimate_call_cost
        from app.services.perplexity_client import SonarResponse

        _classification_cache.clear()
        request = EnrichRequest(
            entity={"Name": "Acme"},
            object_type="Account",
            objective="Find the street",
            schema={"street": "string"},
            max_variations=2,
        )
        reply = SonarResponse(data={"confidence": 0.9}, tokens_used=10, citations=[], model="sonar")
        configs: list = []
        with patch(
            "app.engines.enrichment_orchestrator.query_perplexity",
            new_callable=AsyncMock,
            return_value=reply,
        ) as provider:
            await run_convergence_loop(
                request=request,
                settings=_settings(),
                kb_resolver=_kb_resolver(),
                enricher=_real_provider_enricher(configs),
                inference_rules=[],
                domain_spec=SAMPLE_DOMAIN_SPEC,
                convergence_config=ConvergenceConfig(max_passes=1),
            )
        config = configs[0]
        assert config is not None
        assert config.variations == 2
        assert config.estimated_cost_per_call == estimate_call_cost(
            config.model, config.search_context_size, config.max_tokens, 2
        )
        assert provider.await_count == 2


class TestReviewTargetPropagation:
    """F2: a targeted pass names its unresolved fields in the provider prompt."""

    def _plan(self, mode: str, fields: list[str]):
        from app.engines.meta_prompt_planner import PromptPlan

        return PromptPlan(
            mode=mode, priority_fields=fields, kb_fragment_ids=[], variation_count=3, pass_number=2
        )

    def _request(self) -> EnrichRequest:
        return EnrichRequest(
            entity={"name": "Acme", "industry": None},
            object_type="Account",
            objective="Return the address.",
            schema={"street": "string", "city": "string"},
        )

    def test_targeted_pass_names_schema_gaps_and_keeps_full_schema(self) -> None:
        from app.engines.convergence_controller import ConvergenceState, _build_pass_request

        built = _build_pass_request(
            self._request(), self._plan("targeted", ["street", "industry"]), ConvergenceState(), 2
        )
        assert built.schema_ == {"street": "string", "city": "string"}
        assert built.objective.startswith("Return the address.")
        assert "research them first: street" in built.objective
        # A gap outside the requested schema is not pushed at the provider.
        assert "industry" not in built.objective

    @pytest.mark.parametrize("mode", ["discovery", "verification"])
    def test_other_modes_keep_the_objective(self, mode: str) -> None:
        from app.engines.convergence_controller import ConvergenceState, _build_pass_request

        built = _build_pass_request(
            self._request(), self._plan(mode, ["street"]), ConvergenceState(), 2
        )
        assert built.objective == "Return the address."

    @pytest.mark.asyncio
    async def test_provider_payload_of_targeted_pass_names_the_gap(self) -> None:
        """Verified at the provider: the payload, not just PromptPlan."""
        from unittest.mock import AsyncMock, patch

        from app.engines.convergence.convergence_config import ConvergenceConfig
        from app.services.perplexity_client import SonarResponse

        payloads: list[dict] = []

        async def provider(payload, **_kwargs):
            payloads.append(payload)
            return SonarResponse(
                data={"confidence": 0.9, "city": "Charlotte"},
                tokens_used=10,
                citations=[],
                model="sonar",
            )

        with patch(
            "app.engines.enrichment_orchestrator.query_perplexity",
            new=AsyncMock(side_effect=provider),
        ):
            await run_convergence_loop(
                request=self._request(),
                settings=_settings(),
                kb_resolver=_kb_resolver(),
                enricher=_real_provider_enricher([]),
                inference_rules=[],
                convergence_config=ConvergenceConfig(max_passes=2),
            )

        def user(payload: dict) -> str:
            return next(m["content"] for m in payload["messages"] if m["role"] == "user")

        def system(payload: dict) -> str:
            return next(m["content"] for m in payload["messages"] if m["role"] == "system")

        first, last = payloads[0], payloads[-1]
        assert "PRIORITY THIS PASS" not in user(first)
        assert "research them first: street" in user(last)
        priority_line = user(last).split("research them first:")[1].splitlines()[0]
        assert priority_line.strip() == "street"
        # The full writeback schema still reaches the provider.
        assert "street (string)" in system(last)
        assert "city (string)" in system(last)


class TestReviewImprovementDetection:
    """F3: improvement is measured in c* over the fields that decide readiness."""

    def test_optional_gain_does_not_count_while_required_fields_stall(self) -> None:
        from app.engines.convergence.confidence_tracker import ConfidenceTracker

        tracker = ConfidenceTracker(required_fields=["street"])
        tracker.update_field("street", "1 Main", 0.5, 1, "consensus")
        tracker.update_field("industry", "Plastics", 0.2, 1, "consensus")
        tracker.update_field("street", "1 Main", 0.5, 2, "consensus")
        tracker.update_field("industry", "Plastics", 0.9, 2, "consensus")
        assert tracker.had_meaningful_improvement(0.05) is False

    def test_missing_value_becoming_present_is_an_improvement(self) -> None:
        from app.engines.convergence.confidence_tracker import ConfidenceTracker

        tracker = ConfidenceTracker(required_fields=["street"])
        tracker.update_field("street", None, 0.9, 1, "consensus")
        tracker.update_field("street", "1 Main", 0.9, 2, "consensus")
        assert tracker.had_meaningful_improvement(0.05) is True

    @pytest.mark.parametrize("bad", ["abc", float("nan"), None, True])
    def test_malformed_confidence_is_normalized_at_ingestion(self, bad) -> None:
        from app.engines.convergence.confidence_tracker import ConfidenceTracker

        tracker = ConfidenceTracker()
        tracker.update_field("street", "1 Main", bad, 1, "consensus")
        tracker.update_field("street", "1 Main", 0.6, 2, "consensus")
        assert tracker.get_field_confidence("street") == 0.6
        assert tracker.fields["street"].prior_attempts[-1]["confidence"] == 0.0
        assert tracker.had_meaningful_improvement(0.05) is True


class TestReviewThresholdFallback:
    """F4: a field_thresholds map covers the schema or is absent; absent means 0.85."""

    def test_partial_threshold_map_is_rejected_at_the_request(self) -> None:
        # The consensus engine drops any field the map does not name
        # (apply_field_confidence_thresholds), so a partial map would make an
        # unnamed requested field unfillable. The request refuses it instead.
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="field_thresholds missing"):
            EnrichRequest(
                entity={"name": "Acme"},
                object_type="Account",
                objective="x",
                schema={"street": "string", "city": "string"},
                field_thresholds={"street": 0.9},
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("confidence", "converged"), [(0.84, False), (0.86, True)])
    async def test_absent_map_falls_back_to_loop_threshold(self, confidence, converged) -> None:
        from app.engines.convergence.convergence_config import ConvergenceConfig

        response = await run_convergence_loop(
            request=EnrichRequest(
                entity={"name": "Acme"},
                object_type="Account",
                objective="x",
                schema={"street": "string", "city": "string"},
            ),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_fields_pass({"street": confidence, "city": 0.99})]),
            convergence_config=ConvergenceConfig(max_passes=1),
        )
        assert _tracking(response)["converged"] is converged


class TestReviewFailedPassIsolation:
    """F5: a failed pass's fields never become evidence or convergence."""

    @pytest.mark.asyncio
    async def test_fields_on_a_failed_pass_are_discarded(self) -> None:
        from app.engines.convergence.convergence_config import ConvergenceConfig

        failed_with_fields = EnrichResponse(
            state="failed",
            failure_reason="no_fields_above_consensus_threshold",
            fields={"street": "bogus"},
            confidence=0.99,
            tokens_used=70,
            feature_vector={"per_field_confidence": {"street": 0.99}},
        )
        response = await run_convergence_loop(
            request=EnrichRequest(
                entity={"name": "Acme"},
                object_type="Account",
                objective="x",
                schema={"street": "string"},
            ),
            settings=_settings(),
            kb_resolver=MagicMock(),
            enricher=_scripted_enricher([_fields_pass({"street": 0.5}), failed_with_fields]),
            convergence_config=ConvergenceConfig(max_passes=2),
        )
        assert _tracking(response)["converged"] is False
        assert response.fields["street"] == "v-street"
        assert response.convergence_reason == "insufficient_improvement_delta_0.05"
        assert response.tokens_used == 170
