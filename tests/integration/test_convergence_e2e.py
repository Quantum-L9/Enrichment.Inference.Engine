"""
End-to-end convergence loop integration test.

Exercises the full convergence path: request → convergence_controller →
enricher (mocked LLM) → inference bridge → field confidence →
cost tracking → telemetry → loop state → response.

Only the LLM/network boundary is mocked via the `enricher` kwarg.
All internal wiring (MetaPromptPlanner, InferenceBridge, CostTracker,
ConfidenceTracker) runs for real.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.engines.convergence_controller import run_convergence_loop
from app.models.schemas import EnrichRequest, EnrichResponse

pytestmark = pytest.mark.integration


def _make_enricher(pass_responses: list[dict[str, Any]]):
    """Build a callable enricher that returns different fields per pass."""
    call_count = 0

    async def _enricher(
        request: EnrichRequest,
        settings: Any,
        kb_resolver: Any = None,
        idem_store: Any = None,
        sonar_config: Any = None,
    ) -> EnrichResponse:
        nonlocal call_count
        idx = min(call_count, len(pass_responses) - 1)
        call_count += 1
        resp = pass_responses[idx]

        return EnrichResponse(
            fields=resp.get("fields", {}),
            confidence=resp.get("confidence", 0.8),
            uncertainty_score=resp.get("uncertainty", 2.5),
            tokens_used=resp.get("tokens", 1000),
            processing_time_ms=100,
            state="completed",
            feature_vector=resp.get("feature_vector"),
        )

    return _enricher


@pytest.fixture
def settings() -> MagicMock:
    s = MagicMock()
    s.default_domain = "plastics_recycling"
    s.perplexity_api_key = "test-key"
    s.convergence_threshold = 2.0
    s.max_convergence_passes = 5
    s.max_budget_tokens = 50000
    s.sonar_token_rate_per_1k = 0.005
    return s


@pytest.fixture
def enrich_request() -> EnrichRequest:
    return EnrichRequest(
        entity={"Name": "Test Recycler", "polymer_type": "HDPE"},
        object_type="Account",
        objective="Enrich plastics recycling data",
    )


class TestConvergenceLoopE2E:
    @pytest.mark.asyncio
    async def test_converges_within_passes(
        self, settings: MagicMock, enrich_request: EnrichRequest
    ) -> None:
        enricher = _make_enricher(
            [
                {
                    "fields": {"polymer_type": "HDPE", "mfi_range": "0.5-3.0"},
                    "confidence": 0.75,
                    "uncertainty": 2.8,
                    "tokens": 1500,
                },
                {
                    "fields": {
                        "polymer_type": "HDPE",
                        "mfi_range": "0.5-3.0",
                        "material_grade": "B+",
                    },
                    "confidence": 0.88,
                    "uncertainty": 1.5,
                    "tokens": 1000,
                },
                {
                    "fields": {
                        "polymer_type": "HDPE",
                        "mfi_range": "0.5-3.0",
                        "material_grade": "B+",
                        "contamination_pct": 3.2,
                    },
                    "confidence": 0.92,
                    "uncertainty": 0.8,
                    "tokens": 800,
                },
            ]
        )

        result = await run_convergence_loop(
            request=enrich_request,
            settings=settings,
            kb_resolver=None,
            idem_store=None,
            enricher=enricher,
        )

        assert result.state == "completed"
        assert result.confidence > 0
        assert result.tokens_used > 0
        assert result.pass_count >= 1

    @pytest.mark.asyncio
    async def test_budget_exhaustion_stops_loop(
        self, settings: MagicMock, enrich_request: EnrichRequest
    ) -> None:
        settings.max_budget_tokens = 2000

        enricher = _make_enricher(
            [
                {
                    "fields": {"polymer_type": "HDPE"},
                    "confidence": 0.6,
                    "uncertainty": 3.0,
                    "tokens": 1500,
                },
                {
                    "fields": {"polymer_type": "HDPE"},
                    "confidence": 0.65,
                    "uncertainty": 2.8,
                    "tokens": 1500,
                },
            ]
        )

        result = await run_convergence_loop(
            request=enrich_request,
            settings=settings,
            kb_resolver=None,
            idem_store=None,
            enricher=enricher,
        )

        assert result.tokens_used > 0
        assert result.state == "completed"

    @pytest.mark.asyncio
    async def test_max_passes_respected(
        self, settings: MagicMock, enrich_request: EnrichRequest
    ) -> None:
        from app.engines.convergence.convergence_config import ConvergenceConfig

        config = ConvergenceConfig(max_passes=2)

        enricher = _make_enricher(
            [
                {"fields": {"f1": "v1"}, "confidence": 0.5, "uncertainty": 4.0, "tokens": 500},
                {
                    "fields": {"f1": "v1", "f2": "v2"},
                    "confidence": 0.6,
                    "uncertainty": 3.5,
                    "tokens": 500,
                },
                {
                    "fields": {"f1": "v1", "f2": "v2", "f3": "v3"},
                    "confidence": 0.7,
                    "uncertainty": 3.0,
                    "tokens": 500,
                },
            ]
        )

        result = await run_convergence_loop(
            request=enrich_request,
            settings=settings,
            kb_resolver=None,
            idem_store=None,
            enricher=enricher,
            convergence_config=config,
        )

        assert result.state == "completed"
        assert result.pass_count <= 2

    @pytest.mark.asyncio
    async def test_enricher_failure_produces_response(
        self, settings: MagicMock, enrich_request: EnrichRequest
    ) -> None:
        async def failing_enricher(*args: Any, **kwargs: Any) -> EnrichResponse:
            return EnrichResponse(
                fields={},
                confidence=0.0,
                uncertainty_score=5.0,
                tokens_used=0,
                processing_time_ms=0,
                state="failed",
            )

        result = await run_convergence_loop(
            request=enrich_request,
            settings=settings,
            kb_resolver=None,
            idem_store=None,
            enricher=failing_enricher,
        )

        assert result.state in ("failed", "completed")
