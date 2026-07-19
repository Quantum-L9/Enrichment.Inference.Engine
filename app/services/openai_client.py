"""
app/services/openai_client.py

OpenAI API client for consensus variation diversity.

Uses httpx.AsyncClient directly (no openai SDK) to avoid version conflicts
and to enable precise retry/timeout/circuit-breaker control.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import structlog

logger = structlog.get_logger("openai_client")

_OPENAI_BASE = "https://api.openai.com/v1"
_RETRY_DELAYS = (1.0, 2.0, 4.0)
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class LLMResponseError(Exception):
    """Raised when the LLM returns an unparseable or invalid response."""


class OpenAIClient:
    """
    Async OpenAI chat completions client with retry and circuit breaker.

    Usage:
        client = OpenAIClient(api_key=settings.openai_api_key)
        text = await client.complete("Summarize this company...")
        data = await client.complete_json("Extract fields as JSON...")
    """

    def __init__(self, api_key: str, model: str = "gpt-4o", timeout: int = 60) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._http = httpx.AsyncClient(
            base_url=_OPENAI_BASE,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(timeout),
        )
        self._failure_count = 0
        self._circuit_open = False
        self._availability_cache: tuple[bool, float] | None = None

    async def complete(self, prompt: str, max_tokens: int = 2000, temperature: float = 0.3) -> str:
        """Return the text content of the first completion choice."""
        response = await self._call(
            prompt=prompt, max_tokens=max_tokens, temperature=temperature, json_mode=False
        )
        return str(response["choices"][0]["message"]["content"])

    async def complete_json(self, prompt: str, schema: dict | None = None) -> dict[str, Any]:
        """Return parsed JSON from the model. Raises LLMResponseError if not valid JSON."""
        response = await self._call(prompt=prompt, max_tokens=2000, temperature=0.1, json_mode=True)
        content = response["choices"][0]["message"]["content"]
        try:
            parsed: dict[str, Any] = json.loads(content)
            return parsed
        except json.JSONDecodeError as exc:
            raise LLMResponseError(f"OpenAI returned non-JSON content: {content[:200]}") from exc

    def count_tokens(self, text: str) -> int:
        """Estimate token count using tiktoken cl100k_base encoding."""
        try:
            import tiktoken

            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except ImportError:
            return len(text) // 4

    async def is_available(self) -> bool:
        """Health check via GET /v1/models. Cached 60 seconds."""
        now = time.monotonic()
        if self._availability_cache and (now - self._availability_cache[1]) < 60:
            return self._availability_cache[0]
        try:
            resp = await self._http.get("/models", timeout=5)
            available = resp.status_code == 200
        except Exception:
            available = False
        self._availability_cache = (available, now)
        return available

    async def _call(
        self, prompt: str, max_tokens: int, temperature: float, json_mode: bool
    ) -> dict[str, Any]:
        if self._circuit_open:
            raise LLMResponseError("OpenAI circuit breaker is open")

        body: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        last_exc: Exception | None = None
        for attempt, delay in enumerate(_RETRY_DELAYS, start=1):
            start = time.monotonic()
            try:
                resp = await self._http.post("/chat/completions", json=body)
                latency_ms = int((time.monotonic() - start) * 1000)

                if resp.status_code in _RETRYABLE_STATUS:
                    last_exc = LLMResponseError(f"OpenAI {resp.status_code} on attempt {attempt}")
                    await asyncio.sleep(delay)
                    continue

                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                usage = data.get("usage", {})
                self._failure_count = 0
                logger.info(
                    "openai_call_success",
                    model=self._model,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    latency_ms=latency_ms,
                )
                return data

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                logger.warning("openai_call_network_error", attempt=attempt, error=str(exc))
                await asyncio.sleep(delay)

        self._failure_count += 1
        if self._failure_count >= 5:
            self._circuit_open = True
            logger.error("openai_circuit_breaker_opened", failures=self._failure_count)

        raise LLMResponseError(
            f"OpenAI call failed after {len(_RETRY_DELAYS)} attempts"
        ) from last_exc
