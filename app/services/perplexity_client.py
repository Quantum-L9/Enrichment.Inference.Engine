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

Retry ownership
---------------
EIE is the sole retry owner. The SDK ships `max_retries=2`, so an unconfigured
client turns each EIE attempt into three HTTP requests; every call here is made
through `with_options(max_retries=0)` to collapse that hidden tree.

Timeout ownership
-----------------
The SDK default is `Timeout(connect=5.0, read=900, ...)` — fifteen minutes, far
past any caller budget. The `timeout` argument is applied to the actual
`chat.completions.create()` request, and both it and the retry loop are bounded
by the shared request deadline when one is installed.

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

from .request_deadline import (
    MIN_PROVIDER_ATTEMPT_SECONDS,
    current_deadline,
    provider_attempt_timeout,
)

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
    """Pooled client with SDK auto-retry disabled — EIE owns retries."""
    if api_key not in _clients:
        _clients[api_key] = Perplexity(api_key=api_key, max_retries=0)
        logger.info("client_initialized", sdk_max_retries=0)
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


def _next_attempt_timeout(fallback: float) -> float | None:
    """Timeout for the next attempt, or None when the deadline leaves no room."""
    return provider_attempt_timeout(fallback)


def _sleep_within_deadline(backoff: float) -> bool:
    """Sleep before a retry only if the shared deadline can still absorb it.

    Returns False when the wait would consume the response reserve, in which
    case the caller must stop retrying rather than sleep into the caller's
    timeout.
    """
    deadline = current_deadline()
    if deadline is None:
        time.sleep(backoff)
        return True
    usable = deadline.remaining_for_work() - MIN_PROVIDER_ATTEMPT_SECONDS
    if usable <= 0:
        return False
    time.sleep(min(backoff, usable))
    return not deadline.expired()


def _sync_call(payload: dict[str, Any], api_key: str, timeout: float) -> SonarResponse:
    """Blocking SDK call with retry. Executed via asyncio.to_thread().

    `timeout` is the effective transport timeout for one attempt, already
    derived from the shared request deadline by the caller. It is applied to
    the real `chat.completions.create()` request; `max_retries=0` keeps this
    loop the only retry owner.
    """
    client = _get_client(api_key)
    start = time.monotonic()
    last_err: Exception | None = None
    backoff = 1.0

    for attempt in range(_MAX_RETRIES):
        # E5: never start an attempt the remaining budget cannot accommodate.
        attempt_timeout = _next_attempt_timeout(timeout)
        if attempt_timeout is None:
            logger.warning(
                "provider_attempt_skipped_deadline",
                attempt=attempt,
                elapsed_ms=int((time.monotonic() - start) * 1000),
            )
            raise TimeoutError(
                "provider attempt skipped: remaining request budget below reserve"
            ) from last_err

        try:
            completion = client.with_options(
                timeout=attempt_timeout,
                max_retries=0,
            ).chat.completions.create(**payload)
            return _parse_completion(completion, payload, start)

        except PerplexityError as e:
            last_err = e
            if _should_retry_perplexity(e, attempt, backoff) and _sleep_within_deadline(backoff):
                backoff *= 2
                continue
            raise

        except Exception as e:
            last_err = e
            if attempt < _MAX_RETRIES - 1 and _sleep_within_deadline(backoff):
                backoff *= 2
                continue
            raise

    raise last_err  # unreachable but satisfies type checker


# ── Async interface (unchanged signature from v1) ──────────


async def query_perplexity(
    payload: dict[str, Any],
    api_key: str,
    breaker=None,
    timeout: float = 60,
) -> SonarResponse:
    """
    Async Sonar API call — drop-in replacement for v1.

    Parameters match enrichment_orchestrator.py expectations exactly:
      payload : dict ready for chat.completions.create(**payload)
      api_key : Perplexity API key
      breaker : CircuitBreaker instance (optional)
      timeout : seconds — applied to the real SDK request, and further
                narrowed by the shared request deadline when one is installed

    The blocking SDK call runs on a worker thread, so an outer asyncio timeout
    can always fire. A cancelled `to_thread` does not stop the thread's network
    work, which is why the transport timeout above is mandatory rather than
    merely defensive.
    """
    if breaker and not breaker.allow():
        raise RuntimeError("circuit_open")

    return await asyncio.to_thread(_sync_call, payload, api_key, timeout)
