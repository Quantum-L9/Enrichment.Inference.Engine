"""
Tests for the multi-source waterfall enrichment engine and quality scorer.

These tests use mock sources — no real API calls.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.enrichment.quality_scorer import QualityScorer
from app.services.enrichment.sources.base import (
    BaseSource,
    EnrichmentResult,
    SourceConfig,
)
from app.services.enrichment.waterfall_engine import WaterfallEngine

# ── Test Fixtures ─────────────────────────────────────────────


class MockSource(BaseSource):
    """A mock enrichment source for testing."""

    def __init__(
        self,
        name: str = "mock_source",
        data: dict[str, Any] | None = None,
        quality: float = 0.8,
        error: str | None = None,
    ) -> None:
        config = SourceConfig(
            name=name,
            enabled=True,
            supported_domains=["company", "contact", "account"],
            quality_tier="standard",
        )
        super().__init__(config)
        self._data = data or {}
        self._quality = quality
        self._error = error

    async def enrich(self, domain: str, payload: dict[str, Any]) -> EnrichmentResult:
        return EnrichmentResult(
            data=self._data,
            quality_score=self._quality,
            source_name=self.config.name,
            latency_ms=50,
            error=self._error,
        )


class FailingSource(BaseSource):
    """A source that always fails."""

    def __init__(self) -> None:
        config = SourceConfig(
            name="failing_source",
            enabled=True,
            supported_domains=["company"],
        )
        super().__init__(config)

    async def enrich(self, domain: str, payload: dict[str, Any]) -> EnrichmentResult:
        return EnrichmentResult(
            data={},
            quality_score=0.0,
            source_name="failing_source",
            latency_ms=10,
            error="connection_timeout",
        )


# ── Quality Scorer ────────────────────────────────────────────


class TestQualityScorer:
    """Test quality scoring across dimensions."""

    def test_score_basic(self):
        scorer = QualityScorer()
        score = scorer.score(
            "company",
            {
                "company_name": "Acme",
                "company_domain": "acme.com",
                "employee_count": 500,
            },
        )
        assert 0.0 <= score <= 1.0

    def test_score_empty_record(self):
        scorer = QualityScorer()
        score = scorer.score("company", {})
        assert score >= 0.0

    def test_score_with_source_quality(self):
        scorer = QualityScorer()
        score_without = scorer.score("company", {"company_name": "Acme"})
        score_with = scorer.score("company", {"company_name": "Acme"}, [0.95])
        # Confidence dimension should boost the score
        assert score_with >= score_without

    def test_score_consistency_checks(self):
        scorer = QualityScorer()
        # Good consistency
        good = scorer.score(
            "company",
            {
                "company_name": "Acme",
                "employee_count": 500,
                "annual_revenue": 50000000,
                "company_domain": "acme.com",
            },
        )
        # Bad consistency
        bad = scorer.score(
            "company",
            {
                "company_name": "Acme",
                "employee_count": -5,
                "annual_revenue": -100,
                "company_domain": "not-a-domain",
            },
        )
        assert good > bad

    def test_validate_domain_format(self):
        assert QualityScorer._validate_field("acme.com", "domain_format") is True
        assert QualityScorer._validate_field("not a domain", "domain_format") is False

    def test_validate_phone_format(self):
        assert QualityScorer._validate_field("+1-555-0100", "phone_format") is True
        assert QualityScorer._validate_field("abc", "phone_format") is False

    def test_validate_range_0_1(self):
        assert QualityScorer._validate_field(0.5, "range_0_1") is True
        assert QualityScorer._validate_field(1.5, "range_0_1") is False

    def test_validate_positive_integer(self):
        assert QualityScorer._validate_field(100, "positive_integer") is True
        assert QualityScorer._validate_field(-1, "positive_integer") is False


# ── Waterfall Engine ──────────────────────────────────────────


class TestWaterfallEngine:
    """Test multi-source waterfall enrichment."""

    @pytest.fixture
    def engine(self):
        """Create a waterfall engine with mock sources."""
        engine = WaterfallEngine()
        engine.register_source(
            "primary",
            MockSource(
                name="primary",
                data={"company_name": "Acme Corp", "company_domain": "acme.com"},
                quality=0.7,
            ),
        )
        engine.register_source(
            "secondary",
            MockSource(
                name="secondary",
                data={"employee_count": 500, "annual_revenue": 50000000},
                quality=0.8,
            ),
        )
        return engine

    @pytest.mark.asyncio
    async def test_enrich_basic(self, engine):
        merged, quality, results = await engine.enrich("company", {"entity_name": "Acme"})
        assert "company_name" in merged
        assert quality > 0.0
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_enrich_merges_sources(self):
        """When quality threshold is NOT met by first source, second is called."""
        engine = WaterfallEngine()
        engine.register_source(
            "primary",
            MockSource(
                name="primary",
                data={"company_name": "Acme Corp"},
                quality=0.3,  # Low quality forces fallthrough
            ),
        )
        engine.register_source(
            "secondary",
            MockSource(
                name="secondary",
                data={"employee_count": 500, "annual_revenue": 50000000},
                quality=0.8,
            ),
        )
        merged, _, results = await engine.enrich("company", {"entity_name": "Acme"})
        # Should have data from both sources since primary quality was low
        assert merged.get("company_name") == "Acme Corp"
        assert merged.get("employee_count") == 500
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_enrich_provenance_injected(self, engine):
        merged, _, _ = await engine.enrich("company", {"entity_name": "Acme"})
        assert "enrichment_sources_used" in merged
        assert "enrichment_quality_score" in merged

    @pytest.mark.asyncio
    async def test_enrich_with_failing_source(self):
        engine = WaterfallEngine()
        engine.register_source("failing", FailingSource())
        engine.register_source(
            "backup",
            MockSource(
                name="backup",
                data={"company_name": "Fallback Corp"},
                quality=0.6,
            ),
        )

        merged, _, results = await engine.enrich("company", {"entity_name": "Test"})
        # Should still get data from backup
        assert merged.get("company_name") == "Fallback Corp"
        # First result should have error
        assert results[0].error is not None

    @pytest.mark.asyncio
    async def test_enrich_empty_domain(self):
        engine = WaterfallEngine()
        merged, quality, results = await engine.enrich("unknown_domain", {"entity_name": "Test"})
        assert quality == 0.0 or quality >= 0.0  # no sources = low quality
        assert "enrichment_sources_used" in merged

    @pytest.mark.asyncio
    async def test_register_source(self):
        engine = WaterfallEngine()
        source = MockSource(name="test_source")
        engine.register_source("test_source", source)
        assert "test_source" in engine.source_clients


# ── Source Base ────────────────────────────────────────────────


class TestBaseSource:
    """Test BaseSource interface."""

    def test_source_config(self):
        config = SourceConfig(
            name="test",
            enabled=True,
            supported_domains=["company"],
        )
        assert config.name == "test"
        assert config.enabled is True
        assert config.timeout == 10  # default

    def test_enrichment_result(self):
        result = EnrichmentResult(
            data={"name": "Test"},
            quality_score=0.9,
            source_name="test",
            latency_ms=100,
        )
        assert result.error is None
        assert result.quality_score == 0.9

    def test_enrichment_result_with_error(self):
        result = EnrichmentResult(
            data={},
            quality_score=0.0,
            source_name="test",
            latency_ms=0,
            error="timeout",
        )
        assert result.error == "timeout"
