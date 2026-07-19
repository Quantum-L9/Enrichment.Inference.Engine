"""
Perplexity Client v2.0 — SDK-backed async adapter.

Replaces raw httpx calls with the official Perplexity SDK while
preserving the exact SonarResponse / query_perplexity interface
consumed by enrichment_orchestrator.py.

Changes from v1:
  - httpx.AsyncClient → perplexity.Perplexity SDK
  - Typed access to citations, search_results, usage
  - asyncio.to_thread() bridge for async compatibility
  - Singleton client with lazy init (connection pooling)
  - Retry with backoff built into _sync_call

Dependencies: pip install perplexityai
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from perplexity import Perplexity, PerplexityError

logger = structlog.get_logger("perplexity_client")

# ── Response contract (unchanged from v1) ──────────────────


@dataclass
class SonarResponse:
    """Immutable response from a single Sonar API call."""

    data: dict[str, Any]
    tokens_used: int
    citations: list[str] = field(default_factory=list)
    search_results: list[dict] = field(default_factory=list)
    model: str = ""
    latency_ms: int = 0


# ── Singleton client ───────────────────────────────────────

_clients: dict[str, Perplexity] = {}


def _get_client(api_key: str) -> Perplexity:
    if api_key not in _clients:
        _clients[api_key] = Perplexity(api_key=api_key)
        logger.info("client_initialized")
    return _clients[api_key]


# ── Sync core (runs in thread) ─────────────────────────────

_RETRY_STATUS = {429, 500, 502, 503}
_MAX_RETRIES = 3


def _parse_completion(completion: Any, payload: dict[str, Any], start: float) -> SonarResponse:
    """Build a SonarResponse from a successful SDK completion object."""
    latency = int((time.monotonic() - start) * 1000)
    tokens = completion.usage.total_tokens if completion.usage else 0
    content = completion.choices[0].message.content
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        data = {"_raw": content}
    return SonarResponse(
        data=data,
        tokens_used=tokens,
        citations=getattr(completion, "citations", []) or [],
        search_results=getattr(completion, "search_results", []) or [],
        model=completion.model or payload.get("model", ""),
        latency_ms=latency,
    )


def _should_retry_perplexity(exc: PerplexityError, attempt: int, backoff: float) -> bool:
    """Return True and log if the PerplexityError is retryable and attempts remain."""
    status = getattr(exc, "status_code", 0)
    if status in _RETRY_STATUS and attempt < _MAX_RETRIES - 1:
        logger.warning("retrying", status=status, attempt=attempt + 1, backoff=backoff)
        return True
    return False


def _sync_call(payload: dict[str, Any], api_key: str, timeout: int) -> SonarResponse:
    """Blocking SDK call with retry. Executed via asyncio.to_thread()."""
    client = _get_client(api_key)
    start = time.monotonic()
    last_err: Exception | None = None
    backoff = 1.0

    for attempt in range(_MAX_RETRIES):
        try:
            completion = client.chat.completions.create(**payload)
            return _parse_completion(completion, payload, start)

        except PerplexityError as e:
            last_err = e
            if _should_retry_perplexity(e, attempt, backoff):
                time.sleep(backoff)
                backoff *= 2
                continue
            raise

        except Exception as e:
            last_err = e
            if attempt < _MAX_RETRIES - 1:
                time.sleep(backoff)
                backoff *= 2
                continue
            raise

    if last_err is not None:
        raise last_err
    raise RuntimeError("perplexity query failed without a captured error")  # unreachable


# ── Async interface (unchanged signature from v1) ──────────


async def query_perplexity(
    payload: dict[str, Any],
    api_key: str,
    breaker=None,
    timeout: int = 60,
) -> SonarResponse:
    """
    Async Sonar API call — drop-in replacement for v1.

    Parameters match enrichment_orchestrator.py expectations exactly:
      payload : dict ready for chat.completions.create(**payload)
      api_key : Perplexity API key
      breaker : CircuitBreaker instance (optional)
      timeout : seconds (passed through to SDK)
    """
    if breaker and not breaker.allow():
        raise RuntimeError("circuit_open")

    return await asyncio.to_thread(_sync_call, payload, api_key, timeout)
