"""Integration tests for consensus-based enrichment."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services.enrichment import (
    ConsensusResult,
    KBResolver,
    synthesize,
)
from app.services.enrichment.waterfall_engine import ConsensusEnrichmentResult


@pytest.fixture(autouse=True)
def mock_perplexity_module():
    """Mock the perplexity_client module to avoid import issues.

    The original module object is saved and restored verbatim. Deleting the
    key instead would force a *re-import* on the next access, producing a
    second, distinct module object. Tests that bound ``query_perplexity`` /
    ``_sync_call`` at import time (tests/test_pplx_research.py) would then
    patch the new module while their own references still resolve globals in
    the old one, so the patch silently misses and the real Perplexity SDK is
    called. Restore, never delete.
    """
    module_name = "app.services.perplexity_client"
    sentinel = object()
    original = sys.modules.get(module_name, sentinel)

    mock_module = MagicMock()
    mock_module.SonarResponse = MagicMock
    sys.modules[module_name] = mock_module
    try:
        yield
    finally:
        if original is sentinel:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = original


@pytest.fixture
def mock_perplexity_responses() -> list[dict[str, Any]]:
    """Sample Perplexity responses for consensus testing."""
    return [
        {
            "company_name": "Acme Plastics Inc",
            "industry": "Plastics Recycling",
            "employee_count": 150,
            "annual_revenue": 25000000,
            "polymer_types": ["HDPE", "PP"],
            "confidence": 0.85,
        },
        {
            "company_name": "Acme Plastics Inc",
            "industry": "Plastics Recycling",
            "employee_count": 145,
            "annual_revenue": 25000000,
            "polymer_types": ["HDPE", "PP", "PET"],
            "confidence": 0.82,
        },
        {
            "company_name": "Acme Plastics Inc",
            "industry": "Plastics Manufacturing",
            "employee_count": 150,
            "annual_revenue": 24000000,
            "polymer_types": ["HDPE", "PP"],
            "confidence": 0.78,
        },
    ]


@pytest.fixture
def mock_kb_resolver(tmp_path) -> KBResolver:
    """Create a mock KB resolver with test data."""
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()

    kb_file = kb_dir / "plastics.yaml"
    kb_file.write_text("""
domain: plastics
preamble: Test plastics KB
terminology:
  HDPE: High-Density Polyethylene
fragments:
  - id: test_fragment
    content: Test content
    keywords: [plastics]
    entity_types: [company]
    priority: 10
""")

    return KBResolver(kb_dir=str(kb_dir))


class TestConsensusEnrichmentFlow:
    """Integration tests for the full consensus enrichment flow."""

    @pytest.mark.asyncio
    async def test_consensus_synthesis_standalone(
        self,
        mock_perplexity_responses: list[dict[str, Any]],
    ) -> None:
        """Test consensus synthesis with pre-built responses."""
        result = synthesize(
            payloads=mock_perplexity_responses,
            threshold=0.65,
            total_attempted=3,
        )

        assert isinstance(result, ConsensusResult)
        assert result.contributing_sources == 3
        assert result.confidence >= 0.0
        assert "company_name" in result.fields
        assert result.fields["company_name"] == "Acme Plastics Inc"

    @pytest.mark.asyncio
    async def test_consensus_with_disagreement(self) -> None:
        """Test consensus handles disagreement correctly."""
        payloads = [
            {"name": "Company A", "industry": "Tech"},
            {"name": "Company B", "industry": "Tech"},
            {"name": "Company C", "industry": "Tech"},
        ]

        result = synthesize(payloads, threshold=0.65)

        assert "industry" in result.fields
        assert "name" not in result.fields

    @pytest.mark.asyncio
    async def test_consensus_empty_input(self) -> None:
        """Test consensus with empty input."""
        result = synthesize([], threshold=0.65)

        assert result.fields == {}
        assert result.confidence == 0.0
        assert result.contributing_sources == 0

    @pytest.mark.asyncio
    async def test_uncertainty_application(self) -> None:
        """Test uncertainty is applied correctly."""
        from app.services.enrichment import apply_uncertainty

        result = apply_uncertainty(
            fields={"name": "Test Corp"},
            confidence=0.4,
        )

        assert result.risk_level == "high"
        assert "low_confidence" in result.flags


class TestConsensusWithKBIntegration:
    """Tests for consensus enrichment with KB context."""

    def test_kb_context_affects_prompt_building(
        self,
        mock_kb_resolver: KBResolver,
    ) -> None:
        """Test that KB context is properly resolved and used."""
        kb_ctx = mock_kb_resolver.resolve(
            kb_context="plastics",
            entity={"name": "Test Plastics", "type": "company"},
        )

        assert kb_ctx.domain == "plastics"
        assert len(kb_ctx.fragment_ids) > 0
        assert "HDPE" in kb_ctx.terminology

    def test_kb_terminology_available(
        self,
        mock_kb_resolver: KBResolver,
    ) -> None:
        """Test that KB terminology is accessible."""
        terminology = mock_kb_resolver.get_terminology("plastics")

        assert "HDPE" in terminology
        assert terminology["HDPE"] == "High-Density Polyethylene"


class TestWaterfallEngineConsensusResult:
    """Tests for ConsensusEnrichmentResult dataclass."""

    def test_default_values(self) -> None:
        """Test ConsensusEnrichmentResult has sensible defaults."""
        result = ConsensusEnrichmentResult()

        assert result.fields == {}
        assert result.confidence == 0.0
        assert result.flags == []
        assert result.variations_attempted == 0
        assert result.variations_valid == 0
        assert result.agreement_ratio == 0.0
        assert result.kb_fragments == []
        assert result.quality_score == 0.0
        assert result.risk_level == "low"
        assert result.elapsed_seconds == 0.0

    def test_with_values(self) -> None:
        """Test ConsensusEnrichmentResult with provided values."""
        result = ConsensusEnrichmentResult(
            fields={"name": "Test"},
            confidence=0.85,
            flags=["moderate_confidence"],
            variations_attempted=3,
            variations_valid=3,
            agreement_ratio=0.9,
            kb_fragments=["frag1"],
            quality_score=0.78,
            risk_level="medium",
            elapsed_seconds=1.5,
        )

        assert result.fields == {"name": "Test"}
        assert result.confidence == 0.85
        assert result.flags == ["moderate_confidence"]
        assert result.variations_attempted == 3
        assert result.variations_valid == 3
        assert result.agreement_ratio == 0.9
        assert result.kb_fragments == ["frag1"]
        assert result.quality_score == 0.78
        assert result.risk_level == "medium"
        assert result.elapsed_seconds == 1.5
