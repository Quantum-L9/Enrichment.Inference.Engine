"""Focused provider response parser tests for OpenAI and Anthropic clients."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from app.services.anthropic_client import AnthropicClient
from app.services.openai_client import OpenAIClient


@pytest.mark.asyncio
async def test_openai_client_parses_text_and_json_object(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpenAIClient(api_key="test-key")
    responses: Iterator[dict[str, Any]] = iter(
        [
            {"choices": [{"message": {"content": "Acme summary"}}]},
            {"choices": [{"message": {"content": '{"industry": "recycling"}'}}]},
        ]
    )

    async def fake_call(**_: object) -> dict[str, Any]:
        return next(responses)

    monkeypatch.setattr(client, "_call", fake_call)

    assert await client.complete("summarize") == "Acme summary"
    assert await client.complete_json("extract") == {"industry": "recycling"}


@pytest.mark.asyncio
async def test_anthropic_client_parses_text_and_json_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AnthropicClient(api_key="test-key")
    responses: Iterator[dict[str, Any]] = iter(
        [
            {"content": [{"text": "Acme summary"}]},
            {"content": [{"text": '{"industry": "recycling"}'}]},
        ]
    )

    async def fake_call(**_: object) -> dict[str, Any]:
        return next(responses)

    monkeypatch.setattr(client, "_call", fake_call)

    assert await client.complete("summarize") == "Acme summary"
    assert await client.complete_json("extract") == {"industry": "recycling"}
