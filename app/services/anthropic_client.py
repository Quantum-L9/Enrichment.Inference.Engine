"""
app/services/anthropic_client.py

Anthropic Claude client for consensus variation diversity.

Same interface as OpenAIClient — consensus engine can dispatch to either
provider without branching. Uses httpx.AsyncClient directly.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import structlog

from .openai_client import LLMResponseError

logger = structlog.get_logger("anthropic_client")

_ANTHROPIC_BASE = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"
_RETRY_DELAYS = (1.0, 2.0, 4.0)
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_JSON_INSTRUCTION = (
    "\n\nRespond with valid JSON only. "
    "Do not include markdown code blocks or any text outside the JSON object."
)


def _message_text(response: dict[str, Any]) -> str:
    """Return the text from the first Anthropic content block."""
    content_blocks = response.get("content", [])
    if not isinstance(content_blocks, list) or not content_blocks:
        raise LLMResponseError("Anthropic returned empty content blocks")
    first_block = content_blocks[0]
    if not isinstance(first_block, dict):
        raise LLMResponseError("Anthropic returned an invalid content block")
    text = first_block.get("text")
    if not isinstance(text, str):
        raise LLMResponseError("Anthropic returned non-text content")
    return text


class AnthropicClient:
    """
    Async Anthropic Messages API client with retry and circuit breaker.

    Usage:
        client = AnthropicClient(api_key=settings.anthropic_api_key)
        text = await client.complete("Summarize this company...")
        data = await client.complete_json("Extract fields as JSON...")
    """

    def __init__(
        self, api_key: str, model: str = "claude-3-5-sonnet-20241022", timeout: int = 60
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._failure_count = 0
        self._circuit_open = False
        self._availability_cache: tuple[bool, float] | None = None

    def _client_kwargs(self) -> dict[str, Any]:
        return {
            "base_url": _ANTHROPIC_BASE,
            "headers": {
                "x-api-key": self._api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "Content-Type": "application/json",
            },
            "timeout": httpx.Timeout(self._timeout),
        }

    async def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        """Return the text content of the first response block."""
        data = await self._call(prompt=prompt, max_tokens=max_tokens)
        return _message_text(data)

    async def complete_json(self, prompt: str) -> dict[str, Any]:
        """Return parsed JSON. Raises LLMResponseError if not valid JSON."""
        json_prompt = prompt + _JSON_INSTRUCTION
        text = await self.complete(json_prompt, max_tokens=2000)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(f"Anthropic returned non-JSON content: {text[:200]}") from exc
        if not isinstance(data, dict):
            raise LLMResponseError("Anthropic returned non-object JSON content")
        return data

    async def is_available(self) -> bool:
        """Health check via minimal messages call. Cached 60 seconds."""
        now = time.monotonic()
        if self._availability_cache and (now - self._availability_cache[1]) < 60:
            return self._availability_cache[0]
        try:
            async with httpx.AsyncClient(**self._client_kwargs()) as client:
                resp = await client.post(
                    "/v1/messages",
                    json={
                        "model": self._model,
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                    timeout=5,
                )
            available = resp.status_code in (200, 400)
        except Exception:
            available = False
        self._availability_cache = (available, now)
        return available

    async def _call(self, prompt: str, max_tokens: int) -> dict[str, Any]:
        if self._circuit_open:
            raise LLMResponseError("Anthropic circuit breaker is open")

        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }

        last_exc: Exception | None = None
        for attempt, delay in enumerate(_RETRY_DELAYS, start=1):
            start = time.monotonic()
            try:
                async with httpx.AsyncClient(**self._client_kwargs()) as client:
                    resp = await client.post("/v1/messages", json=body)
                latency_ms = int((time.monotonic() - start) * 1000)

                if resp.status_code in _RETRYABLE_STATUS:
                    last_exc = LLMResponseError(
                        f"Anthropic {resp.status_code} on attempt {attempt}"
                    )
                    await asyncio.sleep(delay)
                    continue

                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    raise LLMResponseError("Anthropic returned a non-object response")
                usage = data.get("usage", {})
                self._failure_count = 0
                logger.info(
                    "anthropic_call_success",
                    model=self._model,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    latency_ms=latency_ms,
                )
                return data

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                logger.warning("anthropic_call_network_error", attempt=attempt, error=str(exc))
                await asyncio.sleep(delay)

        self._failure_count += 1
        if self._failure_count >= 5:
            self._circuit_open = True
            logger.error("anthropic_circuit_breaker_opened", failures=self._failure_count)

        raise LLMResponseError(
            f"Anthropic call failed after {len(_RETRY_DELAYS)} attempts"
        ) from last_exc
