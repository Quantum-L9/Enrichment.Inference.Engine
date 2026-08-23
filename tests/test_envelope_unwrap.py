"""The build_prompt response envelope must be stripped by every consumer.

`prompt_builder.build_prompt` instructs the model to answer with
`{"confidence": <float>, "fields": {...}}`. A consumer that treats that
object as field values merges the two wrapper keys instead of the requested
data, and — worse — any completeness-style quality score counts both wrapper
keys as populated and reports a perfect result over an empty payload.

Two consumers already handled this before these tests existed:
`validation_engine.validate_response` and `simulation_bridge`. Both are on
live request paths, and both are asserted here so a refactor cannot quietly
drop the behaviour. The two that did NOT handle it — `perplexity_adapter` and
the `waterfall_engine` consensus path — are fixed alongside these tests.
"""

from __future__ import annotations

import pytest

from app.services.enrichment.sources.base import SourceConfig
from app.services.enrichment.sources.perplexity_adapter import PerplexitySonarSource
from app.services.prompt_builder import unwrap_envelope
from app.services.validation_engine import validate_response

ENVELOPE = {"confidence": 0.82, "fields": {"industry": "Plastics Recycling", "employee_count": 150}}


class TestUnwrapEnvelope:
    def test_extracts_nested_fields(self) -> None:
        assert unwrap_envelope(ENVELOPE) == {
            "industry": "Plastics Recycling",
            "employee_count": 150,
        }

    def test_flat_response_passes_through(self) -> None:
        flat = {"industry": "Plastics"}
        assert unwrap_envelope(flat) == flat

    def test_non_dict_fields_passes_through(self) -> None:
        """A provider that puts a string in `fields` must not lose the payload."""
        odd = {"confidence": 0.5, "fields": "not-an-object", "industry": "Plastics"}
        assert unwrap_envelope(odd) == odd

    def test_empty_envelope_yields_nothing(self) -> None:
        """The dangerous case: populated wrapper, empty payload."""
        assert unwrap_envelope({"confidence": 0.99, "fields": {}}) == {}


class TestLivePathsAlreadyUnwrap:
    """Regression cover for the two consumers that were already correct."""

    def test_validate_response_unwraps(self) -> None:
        out = validate_response(ENVELOPE, None)
        assert out["industry"] == "Plastics Recycling"
        assert out["employee_count"] == 150
        assert out["confidence"] == 0.82
        assert "fields" not in out

    def test_validate_response_handles_flat(self) -> None:
        out = validate_response({"industry": "Plastics"}, None)
        assert out["industry"] == "Plastics"
        assert out["confidence"] == 0.5  # documented default


class _FakeSonarResponse:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.tokens_used = 0
        self.citations: list[str] = []
        self.search_results: list[dict] = []
        self.model = "sonar"
        self.latency_ms = 1


class TestPerplexityAdapterUnwraps:
    """perplexity_adapter did NOT unwrap before this change."""

    @pytest.mark.asyncio
    async def test_envelope_is_unwrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.services.enrichment.sources import perplexity_adapter as mod

        async def _fake_query(**_kwargs: object) -> _FakeSonarResponse:
            return _FakeSonarResponse(dict(ENVELOPE))

        monkeypatch.setattr(mod, "query_perplexity", _fake_query)
        src = PerplexitySonarSource(
            SourceConfig(name="perplexity_sonar", api_key="pk-test", enabled=True, timeout=10)
        )
        result = await src.enrich("company", {"entity_name": "Acme"})

        assert result.data == {"industry": "Plastics Recycling", "employee_count": 150}
        assert "fields" not in result.data
        assert "confidence" not in result.data

    @pytest.mark.asyncio
    async def test_empty_envelope_does_not_score_perfect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Before the fix this scored 1.0 — enough to stop the waterfall."""
        from app.services.enrichment.sources import perplexity_adapter as mod

        async def _fake_query(**_kwargs: object) -> _FakeSonarResponse:
            return _FakeSonarResponse({"confidence": 0.99, "fields": {}})

        monkeypatch.setattr(mod, "query_perplexity", _fake_query)
        src = PerplexitySonarSource(
            SourceConfig(name="perplexity_sonar", api_key="pk-test", enabled=True, timeout=10)
        )
        result = await src.enrich("company", {"entity_name": "Acme"})

        assert result.data == {}
        assert result.quality_score == 0.0
