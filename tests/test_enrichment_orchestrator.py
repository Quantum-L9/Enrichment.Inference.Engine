"""Tests for app/engines/enrichment_orchestrator.py

Covers: Single-pass enrichment pipeline, query_perplexity call site,
        idempotency, response assembly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.engines.enrichment_orchestrator import enrich_entity
from app.models.schemas import EnrichRequest, EnrichResponse
from app.services.perplexity_client import SonarResponse


class TestEnrichmentOrchestrator:
    """Tests for 10-step single-pass orchestration."""

    @pytest.fixture
    def basic_request(self) -> EnrichRequest:
        return EnrichRequest(
            entity={"Name": "Acme Recycling", "polymer_type": "HDPE"},
            object_type="Account",
            objective="Enrich plastics recycling data",
        )

    @pytest.fixture
    def mock_settings(self) -> MagicMock:
        settings = MagicMock()
        settings.perplexity_api_key = "test-key"
        settings.perplexity_model = "sonar"
        settings.max_concurrent_variations = 3
        settings.default_timeout_seconds = 30
        return settings

    @pytest.fixture
    def mock_kb_resolver(self) -> MagicMock:
        resolver = MagicMock()
        resolver.resolve = MagicMock(
            return_value={
                "context_text": "HDPE: MFI 0.1-25 g/10min",
                "content_hash": "abc123",
                "fragment_ids": ["polymers.hdpe"],
                "kb_files": ["hdpe.yaml"],
            }
        )
        return resolver

    @pytest.fixture
    def sonar_ok(self) -> SonarResponse:
        return SonarResponse(
            data={
                "confidence": 0.85,
                "polymer_type": "HDPE",
                "mfi_range": "0.5-3.0",
            },
            tokens_used=200,
            citations=["https://example.com/hdpe"],
            model="sonar",
        )

    @pytest.mark.asyncio
    async def test_returns_enrich_response(
        self,
        basic_request: EnrichRequest,
        mock_settings: MagicMock,
        mock_kb_resolver: MagicMock,
        sonar_ok: SonarResponse,
    ) -> None:
        with patch(
            "app.engines.enrichment_orchestrator.query_perplexity",
            new_callable=AsyncMock,
            return_value=sonar_ok,
        ):
            response = await enrich_entity(basic_request, mock_settings, mock_kb_resolver)
            assert isinstance(response, EnrichResponse)

    @pytest.mark.asyncio
    async def test_response_has_fields(
        self,
        basic_request: EnrichRequest,
        mock_settings: MagicMock,
        mock_kb_resolver: MagicMock,
        sonar_ok: SonarResponse,
    ) -> None:
        with patch(
            "app.engines.enrichment_orchestrator.query_perplexity",
            new_callable=AsyncMock,
            return_value=sonar_ok,
        ):
            response = await enrich_entity(basic_request, mock_settings, mock_kb_resolver)
            assert response.fields is not None
            assert "polymer_type" in response.fields

    @pytest.mark.asyncio
    async def test_response_has_confidence(
        self,
        basic_request: EnrichRequest,
        mock_settings: MagicMock,
        mock_kb_resolver: MagicMock,
        sonar_ok: SonarResponse,
    ) -> None:
        with patch(
            "app.engines.enrichment_orchestrator.query_perplexity",
            new_callable=AsyncMock,
            return_value=sonar_ok,
        ):
            response = await enrich_entity(basic_request, mock_settings, mock_kb_resolver)
            assert 0.0 <= response.confidence <= 1.0

    @pytest.mark.asyncio
    async def test_response_has_processing_time(
        self,
        basic_request: EnrichRequest,
        mock_settings: MagicMock,
        mock_kb_resolver: MagicMock,
        sonar_ok: SonarResponse,
    ) -> None:
        with patch(
            "app.engines.enrichment_orchestrator.query_perplexity",
            new_callable=AsyncMock,
            return_value=sonar_ok,
        ):
            response = await enrich_entity(basic_request, mock_settings, mock_kb_resolver)
            assert response.processing_time_ms >= 0

    @pytest.mark.asyncio
    async def test_idempotency_key_caching(
        self,
        basic_request: EnrichRequest,
        mock_settings: MagicMock,
        mock_kb_resolver: MagicMock,
        sonar_ok: SonarResponse,
    ) -> None:
        request = basic_request.model_copy(update={"idempotency_key": "test-key-123"})
        cached = EnrichResponse(
            fields={"x": "val"},
            confidence=0.85,
            processing_time_ms=12,
            state="completed",
        )
        idem_store = MagicMock()
        idem_store.get = AsyncMock(return_value=cached.model_dump())
        idem_store.set = AsyncMock()

        with patch(
            "app.engines.enrichment_orchestrator.query_perplexity",
            new_callable=AsyncMock,
            return_value=sonar_ok,
        ) as mock_pplx:
            response = await enrich_entity(
                request,
                mock_settings,
                mock_kb_resolver,
                idem_store,
                idempotency_scope="tenant:acme",
            )
            mock_pplx.assert_not_called()
            idem_store.set.assert_not_called()
            assert response.fields == {"x": "val"}
            assert response.confidence == 0.85
            # EIE-FIX-001: the lookup is owned by the caller's scope and bound
            # to this exact request, never by the caller-chosen key alone.
            scope, key, fingerprint = idem_store.get.await_args.args
            assert (scope, key) == ("tenant:acme", "test-key-123")
            assert len(fingerprint) == 64


class TestEmptyConsensusIsNotCompleted:
    """WU-07 / F-025: replies carrying only empty values do not complete an enrich."""

    @pytest.mark.asyncio
    async def test_all_none_replies_do_not_report_completed(self) -> None:
        request = EnrichRequest(
            entity={"Name": "Acme Recycling"},
            object_type="Account",
            objective="Enrich",
        )
        settings = MagicMock()
        settings.perplexity_api_key = "test-key"
        settings.perplexity_model = "sonar"
        settings.max_concurrent_variations = 2
        settings.default_timeout_seconds = 30
        resolver = MagicMock()
        resolver.resolve = MagicMock(
            return_value={
                "context_text": "",
                "content_hash": "h",
                "fragment_ids": [],
                "kb_files": [],
            }
        )
        empty = SonarResponse(
            data={"confidence": 0.9, "x": None},
            tokens_used=10,
            citations=[],
            model="sonar",
        )
        with patch(
            "app.engines.enrichment_orchestrator.query_perplexity",
            new_callable=AsyncMock,
            return_value=empty,
        ):
            response = await enrich_entity(request, settings, resolver)
        assert response.fields == {}
        assert response.state != "completed"
        assert response.failure_reason


# ── #320 review F1: the provider never runs more calls than authorized ───────


@pytest.mark.asyncio
async def test_sonar_config_cannot_exceed_request_max_variations() -> None:
    """An optimizer recommending six calls runs only the request's two."""
    from app.engines.search_optimizer import SonarConfig

    settings = MagicMock()
    settings.perplexity_api_key = "test-key"
    settings.perplexity_model = "sonar"
    settings.max_concurrent_variations = 3
    settings.default_timeout_seconds = 30
    resolver = MagicMock()
    resolver.resolve = MagicMock(
        return_value={"context_text": "", "content_hash": "h", "fragment_ids": [], "kb_files": []}
    )
    request = EnrichRequest(
        entity={"Name": "Acme"},
        object_type="Account",
        objective="Find the street",
        schema={"street": "string"},
        max_variations=2,
    )
    # A reply that never fills the schema, so no early stop hides the count.
    reply = SonarResponse(data={"confidence": 0.9}, tokens_used=10, citations=[], model="sonar")
    with patch(
        "app.engines.enrichment_orchestrator.query_perplexity",
        new_callable=AsyncMock,
        return_value=reply,
    ) as provider:
        await enrich_entity(request, settings, resolver, sonar_config=SonarConfig(variations=6))
    assert provider.await_count == 2


# ── #320 F4 (revised): partial field_thresholds on the standalone path ───────


def _standalone_settings() -> MagicMock:
    settings = MagicMock()
    settings.perplexity_api_key = "test-key"
    settings.perplexity_model = "sonar"
    settings.max_concurrent_variations = 3
    settings.default_timeout_seconds = 30
    return settings


def _standalone_resolver() -> MagicMock:
    resolver = MagicMock()
    resolver.resolve = MagicMock(
        return_value={"context_text": "", "content_hash": "h", "fragment_ids": [], "kb_files": []}
    )
    return resolver


async def _standalone(field_thresholds, confidence: float, max_variations: int = 2):
    request = EnrichRequest(
        entity={"Name": "Acme"},
        object_type="Account",
        objective="Find the address",
        schema={"street": "string", "city": "string"},
        field_thresholds=field_thresholds,
        max_variations=max_variations,
    )
    reply = SonarResponse(
        data={"confidence": confidence, "street": "1 Main", "city": "Austin"},
        tokens_used=10,
        citations=[],
        model="sonar",
    )
    with patch(
        "app.engines.enrichment_orchestrator.query_perplexity",
        new_callable=AsyncMock,
        return_value=reply,
    ) as provider:
        response = await enrich_entity(request, _standalone_settings(), _standalone_resolver())
    return response, provider.await_count


class TestPartialFieldThresholdsStandalone:
    @pytest.mark.asyncio
    async def test_explicit_field_below_its_override_is_not_filled(self) -> None:
        response, calls = await _standalone({"street": 0.95}, confidence=0.90)
        assert response.state == "completed"
        assert response.fields == {"city": "Austin"}
        # street is unresolved, so the first reply did not short-circuit.
        assert calls == 2

    @pytest.mark.asyncio
    async def test_omitted_field_is_held_to_085_not_consensus_threshold(self) -> None:
        response, _ = await _standalone({"street": 0.5}, confidence=0.80)
        # 0.80 clears consensus_threshold (0.65) but not city's resolved 0.85.
        assert response.fields == {"street": "1 Main"}

    @pytest.mark.asyncio
    async def test_partial_map_met_everywhere_short_circuits(self) -> None:
        response, calls = await _standalone({"street": 0.95}, confidence=0.96)
        assert response.fields == {"street": "1 Main", "city": "Austin"}
        assert calls == 1

    @pytest.mark.asyncio
    async def test_complete_map_is_unchanged(self) -> None:
        response, _ = await _standalone({"street": 0.95, "city": 0.5}, confidence=0.90)
        assert response.fields == {"city": "Austin"}

    @pytest.mark.asyncio
    async def test_absent_map_is_unchanged(self) -> None:
        # Without a map there is no per-field confidence bar; only agreement gates.
        response, _ = await _standalone(None, confidence=0.80)
        assert response.fields == {"street": "1 Main", "city": "Austin"}


# ── Post-#320 F1: request-level confidence integrity ─────────────────────────


async def _enrich_with_replies(replies, *, schema=None, field_thresholds=None, max_variations=3):
    request = EnrichRequest(
        entity={"Name": "Acme"},
        object_type="Account",
        objective="Find the city",
        schema=schema if schema is not None else {"city": "string"},
        field_thresholds=field_thresholds,
        max_variations=max_variations,
    )
    side_effect = list(replies) + [replies[-1]] * 10
    with patch(
        "app.engines.enrichment_orchestrator.query_perplexity",
        new_callable=AsyncMock,
        side_effect=side_effect,
    ) as provider:
        response = await enrich_entity(request, _standalone_settings(), _standalone_resolver())
    return response, provider.await_count


def _reply(confidence, **fields):
    return SonarResponse(
        data={"confidence": confidence, **fields}, tokens_used=10, citations=[], model="sonar"
    )


class TestConfidenceIntegrityStandalone:
    @pytest.mark.asyncio
    async def test_f1_08_singleton_060_is_reported_as_060(self) -> None:
        response, calls = await _enrich_with_replies([_reply(0.60, city="Austin")] * 3)
        assert response.fields == {"city": "Austin"}
        assert response.feature_vector["per_field_confidence"]["city"] == pytest.approx(0.60)
        assert calls == 3  # 0.60 is below the 0.65 agreement bar, so no early stop

    @pytest.mark.asyncio
    async def test_f1_18_invalid_high_raw_confidence_is_not_sufficient(self) -> None:
        _, calls = await _enrich_with_replies([_reply(True, city="Austin")], max_variations=3)
        assert calls == 3

    @pytest.mark.asyncio
    async def test_f1_18_nan_raw_confidence_is_not_sufficient(self) -> None:
        response, calls = await _enrich_with_replies(
            [_reply("nan", city="Austin")], field_thresholds={"city": 0.5}, max_variations=2
        )
        assert calls == 2
        assert "city" not in response.fields

    @pytest.mark.asyncio
    async def test_f1_19_legitimately_sufficient_reply_still_stops_early(self) -> None:
        response, calls = await _enrich_with_replies([_reply(0.95, city="Austin")])
        assert calls == 1
        assert response.feature_vector["per_field_confidence"]["city"] == pytest.approx(0.95)

    @pytest.mark.asyncio
    async def test_f1_17_dissent_cannot_clear_a_095_field_bar(self) -> None:
        replies = [
            _reply(0.96, city="Austin"),
            _reply(0.96, city="Austin"),
            _reply(0.96, city="Dallas"),
        ]
        response, _ = await _enrich_with_replies(
            replies, schema={"city": "string", "street": "string"}, field_thresholds={"city": 0.95}
        )
        assert "city" not in response.fields

    @pytest.mark.asyncio
    async def test_f1_21_failures_are_not_opposing_votes(self) -> None:
        request = EnrichRequest(
            entity={"Name": "Acme"},
            object_type="Account",
            objective="Find the city",
            schema={"city": "string"},
            max_variations=2,
        )
        with patch(
            "app.engines.enrichment_orchestrator.query_perplexity",
            new_callable=AsyncMock,
            # 0.60 is below the 0.65 agreement bar, so the second call is made.
            side_effect=[_reply(0.60, city="Austin"), RuntimeError("timeout")],
        ):
            response = await enrich_entity(request, _standalone_settings(), _standalone_resolver())
        assert response.state == "completed"
        assert response.variation_count == 2
        # The timeout is not a dissenting vote: Austin's agreement is 1/1 over
        # valid replies and its score is its own assertion, not halved.
        assert response.feature_vector["per_field_confidence"]["city"] == pytest.approx(0.60)

    @pytest.mark.asyncio
    async def test_f1_16_missing_confidence_reports_no_success(self) -> None:
        response, _ = await _enrich_with_replies(
            [SonarResponse(data={"city": "Austin"}, tokens_used=10, citations=[], model="sonar")],
            field_thresholds={"city": 0.5},
        )
        assert response.state == "failed"
        assert response.fields == {}

    @pytest.mark.asyncio
    async def test_f1_22_public_response_shape_is_unchanged(self) -> None:
        response, _ = await _enrich_with_replies([_reply(0.95, city="Austin")])
        assert set(response.feature_vector) == {
            "schema_hash",
            "fields_enriched",
            "per_field_confidence",
            "requested_fields",
        }
        assert set(EnrichResponse.model_fields) >= {"fields", "confidence", "feature_vector"}


# ── Post-#320 F2: dispatch obeys exactly one count owner ─────────────────────


async def _dispatch(request, **kwargs):
    reply = SonarResponse(data={"confidence": 0.9}, tokens_used=10, citations=[], model="sonar")
    with patch(
        "app.engines.enrichment_orchestrator.query_perplexity",
        new_callable=AsyncMock,
        return_value=reply,
    ) as provider:
        response = await enrich_entity(
            request, _standalone_settings(), _standalone_resolver(), **kwargs
        )
    return response, provider.await_count


def _street_request(max_variations: int = 5) -> EnrichRequest:
    return EnrichRequest(
        entity={"Name": "Acme"},
        object_type="Account",
        objective="Find the street",
        schema={"street": "string"},
        max_variations=max_variations,
    )


class TestVariationAuthority:
    @pytest.mark.asyncio
    async def test_f2_04_authorized_count_overrides_the_direct_heuristic(self) -> None:
        with patch("app.engines.enrichment_orchestrator.compute_uncertainty", return_value=1):
            _, calls = await _dispatch(_street_request(5), authorized_variations=4)
        assert calls == 4

    @pytest.mark.asyncio
    async def test_f2_03_caller_cap_still_bounds_the_authorized_count(self) -> None:
        _, calls = await _dispatch(_street_request(1), authorized_variations=5)
        assert calls == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("heuristic", "cap", "expected"), [(3, 2, 2), (1, 5, 1)])
    async def test_f2_07_direct_enrich_keeps_its_heuristic_and_caller_cap(
        self, heuristic, cap, expected
    ) -> None:
        with patch(
            "app.engines.enrichment_orchestrator.compute_uncertainty", return_value=heuristic
        ):
            _, calls = await _dispatch(_street_request(cap))
        assert calls == expected

    @pytest.mark.asyncio
    async def test_f2_05_zero_variations_dispatch_nothing_and_say_so(self) -> None:
        from app.engines.search_optimizer import SonarConfig

        with patch("app.engines.enrichment_orchestrator.compute_uncertainty", return_value=3):
            response, calls = await _dispatch(
                _street_request(5), sonar_config=SonarConfig(variations=0)
            )
        assert calls == 0
        assert response.state == "failed"
        assert response.failure_reason == "no_variations_authorized"
        assert response.variation_count == 0

    @pytest.mark.asyncio
    async def test_f2_05_disable_search_still_bypasses_the_provider(self) -> None:
        from app.engines.search_optimizer import SonarConfig

        response, calls = await _dispatch(
            _street_request(5), sonar_config=SonarConfig(disable_search=True, variations=0)
        )
        assert calls == 0
        assert response.state == "completed"

    @pytest.mark.asyncio
    async def test_f2_09_failed_first_reply_never_exceeds_the_authorization(self) -> None:
        with patch(
            "app.engines.enrichment_orchestrator.query_perplexity",
            new_callable=AsyncMock,
            side_effect=[RuntimeError("boom")] + [_reply(0.9)] * 10,
        ) as provider:
            response = await enrich_entity(
                _street_request(5),
                _standalone_settings(),
                _standalone_resolver(),
                authorized_variations=3,
            )
        assert provider.await_count == 3
        assert response.variation_count == 3
