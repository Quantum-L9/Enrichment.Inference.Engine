"""Tests for app/services/perplexity_client.py (query_perplexity / SonarResponse).

Covers: Sonar API integration, citation extraction, rate limiting,
        retry logic. No network — SDK client is mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from perplexity import PerplexityError

from app.services.perplexity_client import (
    SonarResponse,
    _parse_completion,
    _sync_call,
    query_perplexity,
)


def _completion(
    content: str,
    *,
    tokens: int = 1200,
    citations: list[str] | None = None,
    model: str = "sonar",
) -> MagicMock:
    completion = MagicMock()
    completion.usage.total_tokens = tokens
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    completion.choices = [choice]
    completion.citations = citations if citations is not None else ["https://example.com/hdpe"]
    completion.search_results = []
    completion.model = model
    return completion


class TestPerplexityClient:
    """Tests for the current query_perplexity / _sync_call public surface."""

    @pytest.fixture
    def mock_response_success(self) -> SonarResponse:
        return SonarResponse(
            data={"polymer_type": "HDPE", "mfi_range": "0.5-3.0"},
            tokens_used=1200,
            citations=["https://example.com/hdpe"],
            model="sonar",
        )

    @pytest.mark.asyncio
    async def test_search_returns_response(self, mock_response_success: SonarResponse) -> None:
        with patch(
            "app.services.perplexity_client._sync_call",
            return_value=mock_response_success,
        ):
            response = await query_perplexity({"model": "sonar"}, api_key="test-key")
            assert isinstance(response, SonarResponse)

    @pytest.mark.asyncio
    async def test_response_has_content(self, mock_response_success: SonarResponse) -> None:
        with patch(
            "app.services.perplexity_client._sync_call",
            return_value=mock_response_success,
        ):
            response = await query_perplexity({"model": "sonar"}, api_key="test-key")
            assert response.data
            assert response.data.get("polymer_type") == "HDPE"

    @pytest.mark.asyncio
    async def test_citation_extraction(self, mock_response_success: SonarResponse) -> None:
        with patch(
            "app.services.perplexity_client._sync_call",
            return_value=mock_response_success,
        ):
            response = await query_perplexity({"model": "sonar"}, api_key="test-key")
            assert response.citations
            assert "https://example.com/hdpe" in response.citations

    @pytest.mark.asyncio
    async def test_token_usage_tracked(self, mock_response_success: SonarResponse) -> None:
        with patch(
            "app.services.perplexity_client._sync_call",
            return_value=mock_response_success,
        ):
            response = await query_perplexity({"model": "sonar"}, api_key="test-key")
            assert response.tokens_used > 0

    def test_empty_response_handling(self) -> None:
        parsed = _parse_completion(_completion(""), {"model": "sonar"}, start=0.0)
        assert parsed.data == {"_raw": ""}

    @pytest.mark.asyncio
    async def test_timeout_raises_error(self) -> None:
        with (
            patch(
                "app.services.perplexity_client._sync_call",
                side_effect=TimeoutError("Request timed out"),
            ),
            pytest.raises(TimeoutError),
        ):
            await query_perplexity({"model": "sonar"}, api_key="test-key")

    def test_rate_limit_429_retry(self) -> None:
        err = PerplexityError("429 Too Many Requests")
        err.status_code = 429
        success = _completion('{"polymer_type": "HDPE", "mfi_range": "0.5-3.0"}')
        client = MagicMock()
        # Requests go out via `with_options(timeout=..., max_retries=0)`; the
        # double returns itself so the configured side_effect and call_count
        # still measure the real provider calls.
        client.with_options.return_value = client
        client.chat.completions.create.side_effect = [err, success]

        with (
            patch("app.services.perplexity_client._get_client", return_value=client),
            patch("app.services.perplexity_client.time.sleep"),
        ):
            response = _sync_call({"model": "sonar"}, "test-key", 60)

        assert response.data["polymer_type"] == "HDPE"
        assert client.chat.completions.create.call_count == 2
        assert response.tokens_used == 1200
        # EIE owns the retry: neither attempt may delegate one to the SDK.
        assert client.with_options.call_count == 2
        for call in client.with_options.call_args_list:
            assert call.kwargs["max_retries"] == 0
