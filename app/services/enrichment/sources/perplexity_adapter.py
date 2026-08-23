"""
Perplexity Sonar enrichment source adapter.

Wraps the existing perplexity_client.query_perplexity() function as a
BaseSource implementation for use in the WaterfallEngine.

This adapter does NOT duplicate the Perplexity SDK logic — it delegates
to the existing singleton client in app/services/perplexity_client.py.

L9 Architecture Note:
    This module bridges the existing Perplexity client into the
    multi-source enrichment interface. It never imports FastAPI.
"""

from __future__ import annotations

from typing import Any

import structlog

from ...perplexity_client import SonarResponse, query_perplexity
from ...prompt_builder import build_prompt, unwrap_envelope
from ..sources.base import BaseSource, EnrichmentResult, SourceConfig

logger = structlog.get_logger("perplexity_adapter")


class PerplexitySonarSource(BaseSource):
    """
    BaseSource adapter for Perplexity Sonar API.

    Translates the BaseSource.enrich() contract into the existing
    query_perplexity() call, preserving circuit breaker integration.
    """

    def __init__(
        self,
        config: SourceConfig,
        breaker: Any | None = None,
    ) -> None:
        super().__init__(config)
        self._breaker = breaker

    async def enrich(self, domain: str, payload: dict[str, Any]) -> EnrichmentResult:
        """
        Perform enrichment via Perplexity Sonar.

        Builds a prompt from the payload, calls the existing
        query_perplexity() function, and returns an EnrichmentResult.
        """
        start = self._now_ms()

        if not self.config.api_key:
            return EnrichmentResult(
                data={},
                quality_score=0.0,
                source_name=self.config.name,
                latency_ms=0,
                error="no_api_key_configured",
            )

        try:
            # Build the Sonar API payload using the existing prompt builder
            entity = {
                "entity_name": payload.get("entity_name", ""),
                "entity_type": payload.get("entity_type", domain),
                "location": payload.get("location", ""),
            }
            # Include all known fields for richer prompts
            entity.update({k: v for k, v in payload.items() if v not in (None, "", [], {})})
            sonar_payload = build_prompt(
                entity=entity,
                object_type=domain,
                objective="enrich",
            )

            response: SonarResponse = await query_perplexity(
                payload=sonar_payload,
                api_key=self.config.api_key,
                breaker=self._breaker,
                timeout=self.config.timeout,
            )

            latency = self._now_ms() - start

            # Strip the {"confidence", "fields"} envelope that build_prompt
            # asked for. Without this the caller merges the two wrapper keys
            # and the completeness score below counts them as populated
            # fields, reporting 1.0 over an empty payload.
            data = unwrap_envelope(response.data)
            non_empty = sum(1 for v in data.values() if v not in (None, "", [], {}))
            total = max(len(data), 1)
            quality = min(non_empty / total, 1.0)

            return EnrichmentResult(
                data=data,
                quality_score=round(quality, 3),
                source_name=self.config.name,
                latency_ms=latency,
            )

        except Exception as exc:
            latency = self._now_ms() - start
            logger.error(
                "perplexity_source_error",
                domain=domain,
                error=str(exc),
                latency_ms=latency,
            )
            return EnrichmentResult(
                data={},
                quality_score=0.0,
                source_name=self.config.name,
                latency_ms=latency,
                error=str(exc),
            )
