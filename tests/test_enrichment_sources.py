"""
Tests for enrichment source adapters.

Validates that each source adapter:
1. Implements BaseSource contract
2. Handles missing API keys gracefully
3. Handles disabled state
4. Handles unsupported domains
5. Maps response fields to canonical names
"""

from __future__ import annotations

import pytest

from app.services.enrichment.sources import SOURCE_REGISTRY, llm_base
from app.services.enrichment.sources.anthropic_adapter import AnthropicSource
from app.services.enrichment.sources.apollo import ApolloSource
from app.services.enrichment.sources.base import BaseSource, SourceConfig
from app.services.enrichment.sources.clearbit import ClearbitSource
from app.services.enrichment.sources.hunter import HunterSource
from app.services.enrichment.sources.llm_base import LLMSource
from app.services.enrichment.sources.openai_adapter import OpenAISource
from app.services.enrichment.sources.zoominfo import ZoomInfoSource

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(name: str, enabled: bool = True, api_key: str = "test-key") -> SourceConfig:
    return SourceConfig(
        name=name,
        enabled=enabled,
        api_endpoint="https://api.example.com",
        auth_type="api_key",
        api_key=api_key,
        timeout=10,
        retry_count=1,
        supported_domains=["company", "contact"],
        quality_tier="standard",
    )


# ---------------------------------------------------------------------------
# Registry Tests
# ---------------------------------------------------------------------------


class TestSourceRegistry:
    """Verify all sources are registered."""

    def test_registry_has_all_sources(self) -> None:
        expected = {
            "perplexity_sonar",
            "clearbit",
            "zoominfo",
            "apollo",
            "hunter",
            "openai",
            "anthropic",
        }
        assert expected == set(SOURCE_REGISTRY.keys())

    def test_all_registry_values_are_base_source(self) -> None:
        for name, cls in SOURCE_REGISTRY.items():
            assert issubclass(cls, BaseSource), f"{name} is not a BaseSource"


# ---------------------------------------------------------------------------
# Contract Tests — every adapter must handle these edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_cls,name",
    [
        (ClearbitSource, "clearbit"),
        (ZoomInfoSource, "zoominfo"),
        (ApolloSource, "apollo"),
        (HunterSource, "hunter"),
        (OpenAISource, "openai"),
        (AnthropicSource, "anthropic"),
    ],
)
class TestSourceContract:
    """Contract tests that every source adapter must pass."""

    @pytest.mark.asyncio
    async def test_disabled_source_returns_error(self, source_cls: type, name: str) -> None:
        config = _make_config(name, enabled=False)
        src = source_cls(config=config)
        result = await src.enrich("company", {"company_name": "Test"})
        assert result.quality_score == 0.0
        assert result.error == "source_disabled"

    @pytest.mark.asyncio
    async def test_missing_api_key_returns_error(self, source_cls: type, name: str) -> None:
        config = _make_config(name, api_key="")
        src = source_cls(config=config)
        result = await src.enrich("company", {"company_name": "Test"})
        assert result.quality_score == 0.0
        assert result.error == "missing_api_key"

    @pytest.mark.asyncio
    async def test_result_has_source_name(self, source_cls: type, name: str) -> None:
        config = _make_config(name, enabled=False)
        src = source_cls(config=config)
        result = await src.enrich("company", {"company_name": "Test"})
        assert result.source_name == name

    @pytest.mark.asyncio
    async def test_result_has_latency(self, source_cls: type, name: str) -> None:
        config = _make_config(name, enabled=False)
        src = source_cls(config=config)
        result = await src.enrich("company", {"company_name": "Test"})
        assert isinstance(result.latency_ms, int)
        assert result.latency_ms >= 0


# ---------------------------------------------------------------------------
# Hunter-specific: contact-only domain
# ---------------------------------------------------------------------------


class TestHunterDomainRestriction:
    """Hunter only supports contact domain."""

    @pytest.mark.asyncio
    async def test_company_domain_returns_unsupported(self) -> None:
        config = _make_config("hunter")
        src = HunterSource(config=config)
        result = await src.enrich("company", {"company_name": "Test"})
        assert result.error == "unsupported_domain"

    @pytest.mark.asyncio
    async def test_contact_without_identifier_returns_error(self) -> None:
        config = _make_config("hunter")
        src = HunterSource(config=config)
        result = await src.enrich("contact", {})
        assert result.error == "missing_identifier"


# ---------------------------------------------------------------------------
# Apollo-specific: missing identifier
# ---------------------------------------------------------------------------


class TestApolloMissingIdentifier:
    """Apollo needs company_domain for company enrichment."""

    @pytest.mark.asyncio
    async def test_company_without_domain_returns_error(self) -> None:
        config = _make_config("apollo")
        src = ApolloSource(config=config)
        result = await src.enrich("company", {"company_name": "Test"})
        assert result.error == "missing_company_domain"


# ---------------------------------------------------------------------------
# LLM source adapters — OpenAI / Anthropic
# ---------------------------------------------------------------------------


class _FakeLLMClient:
    """Stands in for OpenAIClient / AnthropicClient without network IO."""

    def __init__(self, result=None, raises: Exception | None = None) -> None:
        self._result = result if result is not None else {}
        self._raises = raises
        self.prompts: list[str] = []

    async def complete_json(self, prompt: str):
        self.prompts.append(prompt)
        if self._raises is not None:
            raise self._raises
        return self._result


@pytest.mark.parametrize(
    "source_cls,name",
    [(OpenAISource, "openai"), (AnthropicSource, "anthropic")],
)
class TestLLMSources:
    """Behaviour shared by the two chat-completion adapters."""

    @pytest.mark.asyncio
    async def test_returns_provider_data(self, source_cls: type, name: str) -> None:
        client = _FakeLLMClient({"industry": "Plastics Recycling", "employee_count": 150})
        src = source_cls(config=_make_config(name), client=client)
        result = await src.enrich("company", {"company_name": "Acme"})
        assert result.error is None
        assert result.data["industry"] == "Plastics Recycling"
        assert result.source_name == name

    @pytest.mark.asyncio
    async def test_quality_reflects_completeness(self, source_cls: type, name: str) -> None:
        client = _FakeLLMClient({"a": "x", "b": "", "c": None, "d": "y"})
        src = source_cls(config=_make_config(name), client=client)
        result = await src.enrich("company", {"company_name": "Acme"})
        assert result.quality_score == 0.5  # 2 of 4 fields populated

    @pytest.mark.asyncio
    async def test_prompt_includes_entity(self, source_cls: type, name: str) -> None:
        client = _FakeLLMClient({"ok": 1})
        src = source_cls(config=_make_config(name), client=client)
        await src.enrich("company", {"company_name": "Alpha Recyclers"})
        assert client.prompts, "adapter must call the provider exactly once"
        assert "Alpha Recyclers" in client.prompts[0]

    @pytest.mark.asyncio
    async def test_provider_error_is_returned_not_raised(self, source_cls: type, name: str) -> None:
        """BaseSource contract: network failures never propagate."""
        client = _FakeLLMClient(raises=RuntimeError("upstream 503"))
        src = source_cls(config=_make_config(name), client=client)
        result = await src.enrich("company", {"company_name": "Acme"})
        assert result.error == "upstream 503"
        assert result.quality_score == 0.0
        assert result.data == {}

    @pytest.mark.asyncio
    async def test_non_object_response_is_rejected(self, source_cls: type, name: str) -> None:
        client = _FakeLLMClient(result=["not", "an", "object"])
        src = source_cls(config=_make_config(name), client=client)
        result = await src.enrich("company", {"company_name": "Acme"})
        assert result.error == "non_object_response"
        assert result.quality_score == 0.0

    def test_model_is_not_overridden_by_default(self, source_cls: type, name: str) -> None:
        """Adapter wiring must not silently pick a model for the client."""
        src = source_cls(config=_make_config(name))
        assert src._model is None

    def test_resolve_client_builds_real_client_from_config(
        self, source_cls: type, name: str
    ) -> None:
        """With no injected client, the adapter constructs one from config.

        Constructing the client opens no connection, so this stays hermetic.
        """
        src = source_cls(config=_make_config(name, api_key="sk-test"))
        client = src._resolve_client()
        assert client._api_key == "sk-test"
        assert client._timeout == 10
        # No model was requested, so the client keeps its own default.
        assert client._model

    def test_resolve_client_honours_explicit_model(self, source_cls: type, name: str) -> None:
        src = source_cls(config=_make_config(name), model="explicit-model-id")
        assert src._resolve_client()._model == "explicit-model-id"

    def test_resolve_client_prefers_injected_client(self, source_cls: type, name: str) -> None:
        sentinel = _FakeLLMClient({"a": 1})
        src = source_cls(config=_make_config(name), client=sentinel)
        assert src._resolve_client() is sentinel


def test_llm_subclass_without_client_class_is_rejected() -> None:
    """A provider adapter that forgets client_class must fail at import time.

    Without this the omission surfaces as an AttributeError on the first live
    request, which is the worst place to discover it.
    """
    # type() rather than a class statement: what is under test is the
    # subclass creation itself, and a class statement would bind a name
    # nothing goes on to use.
    with pytest.raises(TypeError, match="client_class"):
        type("_NoClient", (LLMSource,), {})


@pytest.mark.parametrize(
    "source_cls,name",
    [(OpenAISource, "openai"), (AnthropicSource, "anthropic")],
)
class TestLLMEnvelopeAndClientReuse:
    """Regressions for the prompted-response envelope and client lifetime."""

    @pytest.mark.asyncio
    async def test_prompted_envelope_is_unwrapped(self, source_cls: type, name: str) -> None:
        """build_prompt asks for {"confidence", "fields"} — merge the fields.

        Returning the envelope verbatim would merge two literal keys instead
        of the requested values.
        """
        client = _FakeLLMClient(
            {
                "confidence": 0.82,
                "fields": {"industry": "Plastics Recycling", "employee_count": 150},
            }
        )
        src = source_cls(config=_make_config(name), client=client)
        result = await src.enrich("company", {"company_name": "Acme"})
        assert result.data == {"industry": "Plastics Recycling", "employee_count": 150}
        assert "confidence" not in result.data
        assert "fields" not in result.data

    @pytest.mark.asyncio
    async def test_empty_envelope_does_not_score_perfect(self, source_cls: type, name: str) -> None:
        """The dangerous case: a populated envelope wrapping nothing.

        Before unwrapping, both envelope keys counted as populated fields and
        scored 1.0 — high enough to stop the waterfall having merged nothing.
        """
        client = _FakeLLMClient({"confidence": 0.99, "fields": {}})
        src = source_cls(config=_make_config(name), client=client)
        result = await src.enrich("company", {"company_name": "Acme"})
        assert result.data == {}
        assert result.quality_score == 0.0

    @pytest.mark.asyncio
    async def test_flat_response_still_works(self, source_cls: type, name: str) -> None:
        """A provider answering with a flat object is passed through."""
        client = _FakeLLMClient({"industry": "Plastics"})
        src = source_cls(config=_make_config(name), client=client)
        result = await src.enrich("company", {"company_name": "Acme"})
        assert result.data == {"industry": "Plastics"}

    def test_client_is_constructed_once(self, source_cls: type, name: str) -> None:
        """The client must be reused so its circuit breaker can accumulate.

        Both provider clients hold _failure_count on the instance; a fresh
        client per request resets it and the breaker never trips.
        """
        src = source_cls(config=_make_config(name, api_key="sk-test"))
        assert src._resolve_client() is src._resolve_client()

    @pytest.mark.asyncio
    async def test_provider_error_text_is_not_logged(
        self, source_cls: type, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Raw model output must not reach the log; both clients embed it."""
        secret = "PARTNER-CONFIDENTIAL-abc123"
        captured: dict[str, object] = {}

        def _capture(event: str, **kwargs: object) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(llm_base.logger, "error", _capture)
        client = _FakeLLMClient(raises=ValueError(f"non-JSON content: {secret}"))
        src = source_cls(config=_make_config(name), client=client)
        result = await src.enrich("company", {"company_name": "Acme"})

        assert secret not in " ".join(str(v) for v in captured.values())
        assert captured.get("error_type") == "ValueError"
        # The message still reaches the caller, just not the log.
        assert secret in (result.error or "")
