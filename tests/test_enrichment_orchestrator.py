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
            response = await enrich_entity(request, mock_settings, mock_kb_resolver, idem_store)
            mock_pplx.assert_not_called()
            idem_store.set.assert_not_called()
            assert response.fields == {"x": "val"}
            assert response.confidence == 0.85
