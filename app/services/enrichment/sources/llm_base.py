"""
Shared adapter behaviour for chat-completion LLM enrichment sources.

OpenAI and Anthropic differ only in which client they call; the surrounding
contract — disabled/missing-key guards, prompt construction, quality scoring,
latency accounting and never-raise error handling — is identical. That contract
lives here once so the two provider adapters stay thin.

Prompt construction delegates to ``prompt_builder.build_prompt`` rather than
duplicating it. That builder targets Perplexity's chat-completion payload
shape, so its ``messages`` are flattened into the single prompt string the
OpenAI/Anthropic clients accept. Reusing it keeps schema and KB-context
handling defined in exactly one place.

L9 Architecture Note:
    This module is chassis-agnostic. Sources never import FastAPI.
"""

from __future__ import annotations

from typing import Any, ClassVar

import structlog

from ...prompt_builder import build_prompt
from .base import BaseSource, EnrichmentResult, SourceConfig

logger = structlog.get_logger("llm_source")


def flatten_prompt(domain: str, payload: dict[str, Any]) -> str:
    """Build an enrichment prompt as a single string.

    ``build_prompt`` returns a Perplexity chat-completion payload; the
    system and user turns are concatenated so providers taking a plain
    prompt string receive the same instructions.
    """
    entity = {
        "entity_name": payload.get("entity_name", ""),
        "entity_type": payload.get("entity_type", domain),
        "location": payload.get("location", ""),
    }
    entity.update({k: v for k, v in payload.items() if v not in (None, "", [], {})})

    built = build_prompt(entity=entity, object_type=domain, objective="enrich")
    parts = [
        str(message.get("content", ""))
        for message in built.get("messages", [])
        if message.get("role") in {"system", "user"}
    ]
    return "\n\n".join(part for part in parts if part)


def score_completeness(data: dict[str, Any]) -> float:
    """Quality score from field completeness."""
    non_empty = sum(1 for value in data.values() if value not in (None, "", [], {}))
    total = max(len(data), 1)
    return round(min(non_empty / total, 1.0), 3)


def unwrap_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the enrichment fields from a prompted provider response.

    ``build_prompt`` instructs the model to answer with an envelope:
    ``{"confidence": 0.82, "fields": {...}}``. Returning that envelope
    verbatim would have two bad consequences. The waterfall would merge the
    literal keys ``confidence`` and ``fields`` instead of the requested
    values, and :func:`score_completeness` would see two populated keys and
    report a perfect 1.0 while zero real fields were merged — a score high
    enough to stop the waterfall from trying any further source.

    The provider's self-reported ``confidence`` is deliberately NOT used as
    the quality score. Quality here means "how much did we actually get",
    measured the same way for every source; a model asserting 0.99 about an
    empty payload must not outrank a source that returned real data.

    A response without a dict ``fields`` key is passed through unchanged, so
    a provider that answers with a flat object still works.
    """
    fields = raw.get("fields")
    return fields if isinstance(fields, dict) else raw


class LLMSource(BaseSource):
    """Template adapter for a chat-completion LLM provider.

    A subclass declares :attr:`client_class` and nothing else. Everything
    else — construction, client resolution, and the ``enrich`` contract
    including the guarantee that network failures are *returned* rather than
    raised — is fixed here.

    Client construction is shared because both providers take the same
    arguments: ``api_key``, ``timeout``, and an optional ``model``. Spelling
    that out per provider produced two files identical except for a name.
    """

    #: Provider client class, constructed with ``api_key`` and ``timeout``
    #: (plus ``model``, only when a caller explicitly asked for one).
    client_class: ClassVar[type[Any]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Fail at import time, not at first request, on a missing client."""
        super().__init_subclass__(**kwargs)
        if not getattr(cls, "client_class", None):
            raise TypeError(f"{cls.__name__} must declare a client_class")

    def __init__(
        self,
        config: SourceConfig,
        client: Any | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(config)
        self._client = client
        self._model = model

    def _resolve_client(self) -> Any:
        """Return the injected client, or build one from config.

        Injection exists so tests can exercise an adapter without network IO;
        production callers pass nothing and get a client built from the
        configured key and timeout.
        """
        if self._client is not None:
            return self._client
        kwargs: dict[str, Any] = {
            "api_key": self.config.api_key or "",
            "timeout": self.config.timeout,
        }
        # Only override the model when a caller asks for one; otherwise the
        # client's own documented default applies. Adapter wiring is not the
        # place to make a model-selection decision.
        if self._model is not None:
            kwargs["model"] = self._model
        # Cache it. Both provider clients keep _failure_count and _circuit_open
        # on the instance, so building a fresh client per request would reset
        # the counter every time and the circuit breaker could never reach its
        # threshold during a sustained outage.
        self._client = self.client_class(**kwargs)
        return self._client

    async def complete_json(self, prompt: str) -> dict[str, Any]:
        """Call the provider and return parsed JSON."""
        return await self._resolve_client().complete_json(prompt)

    async def enrich(self, domain: str, payload: dict[str, Any]) -> EnrichmentResult:
        start = self._now_ms()

        if not self.config.enabled:
            return EnrichmentResult(
                data={},
                quality_score=0.0,
                source_name=self.config.name,
                latency_ms=0,
                error="source_disabled",
            )

        if not self.config.api_key:
            return EnrichmentResult(
                data={},
                quality_score=0.0,
                source_name=self.config.name,
                latency_ms=0,
                error="missing_api_key",
            )

        try:
            data = await self.complete_json(flatten_prompt(domain, payload))
        except Exception as exc:
            latency = self._now_ms() - start
            # Log the exception TYPE, not str(exc): both clients embed up to
            # 200 characters of raw model output in LLMResponseError, and that
            # output may carry partner data. The message still reaches the
            # caller on the result below; it just does not get persisted here.
            logger.error(
                "llm_source_error",
                source=self.config.name,
                domain=domain,
                error_type=type(exc).__name__,
                latency_ms=latency,
            )
            return EnrichmentResult(
                data={},
                quality_score=0.0,
                source_name=self.config.name,
                latency_ms=latency,
                error=str(exc),
            )

        if not isinstance(data, dict):
            return EnrichmentResult(
                data={},
                quality_score=0.0,
                source_name=self.config.name,
                latency_ms=self._now_ms() - start,
                error="non_object_response",
            )

        fields = unwrap_fields(data)
        return EnrichmentResult(
            data=fields,
            quality_score=score_completeness(fields),
            source_name=self.config.name,
            latency_ms=self._now_ms() - start,
        )
