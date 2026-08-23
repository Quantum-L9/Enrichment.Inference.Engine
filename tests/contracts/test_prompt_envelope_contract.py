"""
Prompt Response Envelope Contract
Source: app/services/prompt_builder.py — the system prompt's declared reply shape.
Markers: unit

`build_prompt` instructs the model to answer with
`{"confidence": <float>, "fields": {...}}`. That envelope is a contract between
the builder and every consumer of a completion built from it: a consumer that
treats the envelope as field values merges the two wrapper keys instead of the
requested data, and any completeness-style quality score counts both wrapper
keys as populated and reports a perfect result over an empty payload.

Four consumers exist. Two stripped the envelope before this file existed —
`validation_engine.validate_response` and `simulation_bridge`, both on live
request paths — and both are asserted here so a refactor cannot quietly drop
the behaviour. The two that did not, `perplexity_adapter` and the
`waterfall_engine` consensus path, are fixed alongside these tests.

This lives under tests/contracts/ because the envelope is a declared response
contract, not an implementation detail of any one adapter.

Every app import is deferred into a test body on purpose. The L9 Constitution
Gate collects this directory in a lean environment that does not install the
provider SDKs, and a module-level `from app.services.enrichment...` import
pulls the package __init__ -> waterfall_engine -> sources -> perplexity_client
-> `import perplexity`, which fails collection there even though these tests
are deselected by that gate's marker expression. Deferring keeps the module
importable everywhere without weakening an assertion.
"""

from __future__ import annotations

import pytest

ENVELOPE = {"confidence": 0.82, "fields": {"industry": "Plastics Recycling", "employee_count": 150}}


class TestUnwrapEnvelope:
    @staticmethod
    def _unwrap():
        from app.services.prompt_builder import unwrap_envelope

        return unwrap_envelope

    def test_extracts_nested_fields(self) -> None:
        assert self._unwrap()(ENVELOPE) == {
            "industry": "Plastics Recycling",
            "employee_count": 150,
        }

    def test_flat_response_passes_through(self) -> None:
        flat = {"industry": "Plastics"}
        assert self._unwrap()(flat) == flat

    def test_non_dict_fields_passes_through(self) -> None:
        """A provider that puts a string in `fields` must not lose the payload."""
        odd = {"confidence": 0.5, "fields": "not-an-object", "industry": "Plastics"}
        assert self._unwrap()(odd) == odd

    def test_empty_envelope_yields_nothing(self) -> None:
        """The dangerous case: populated wrapper, empty payload."""
        assert self._unwrap()({"confidence": 0.99, "fields": {}}) == {}


class TestLivePathsAlreadyUnwrap:
    """Regression cover for the two consumers that were already correct."""

    def test_validate_response_unwraps(self) -> None:
        from app.services.validation_engine import validate_response

        out = validate_response(ENVELOPE, None)
        assert out["industry"] == "Plastics Recycling"
        assert out["employee_count"] == 150
        assert out["confidence"] == 0.82
        assert "fields" not in out

    def test_validate_response_handles_flat(self) -> None:
        from app.services.validation_engine import validate_response

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
        from app.services.enrichment.sources.base import SourceConfig
        from app.services.enrichment.sources.perplexity_adapter import PerplexitySonarSource

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
        from app.services.enrichment.sources.base import SourceConfig
        from app.services.enrichment.sources.perplexity_adapter import PerplexitySonarSource

        async def _fake_query(**_kwargs: object) -> _FakeSonarResponse:
            return _FakeSonarResponse({"confidence": 0.99, "fields": {}})

        monkeypatch.setattr(mod, "query_perplexity", _fake_query)
        src = PerplexitySonarSource(
            SourceConfig(name="perplexity_sonar", api_key="pk-test", enabled=True, timeout=10)
        )
        result = await src.enrich("company", {"entity_name": "Acme"})

        assert result.data == {}
        assert result.quality_score == 0.0


class TestWaterfallConsensusUnwraps:
    """The consensus path did NOT unwrap before this change.

    It is also entirely uncovered by the existing suite, which is how a
    malformed import in this exact function survived a full green run: the
    imports are function-local, so nothing executes them until the method is
    called. These tests call it.
    """

    @pytest.mark.asyncio
    async def test_variations_are_unwrapped_before_consensus(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every variation must be unwrapped before it reaches consensus.

        A `perplexity_api_key` is REQUIRED here and is not incidental setup.
        `enrich_with_consensus` looks up `source_clients["perplexity_sonar"]`
        and, when it is absent, returns early with
        `flags=["error:no_perplexity_source"]` and `fields={}` before any
        variation runs. An engine built with no key therefore satisfies every
        "wrapper key is absent" assertion vacuously, against an empty dict.
        The positive assertion below is what forces the path to actually run.

        Only the `app.services.perplexity_client` attribute is patched: the
        import inside `enrich_with_consensus` is function-local, so patching
        the `waterfall_engine` module attribute would bind nothing.
        """
        from app.services.enrichment import waterfall_engine as we

        async def _fake_query(**_kwargs: object) -> _FakeSonarResponse:
            return _FakeSonarResponse(
                {"confidence": 0.9, "fields": {"company_industry": "Plastics Recycling"}}
            )

        monkeypatch.setattr(
            "app.services.perplexity_client.query_perplexity", _fake_query, raising=False
        )

        engine = we.WaterfallEngine(perplexity_api_key="pk-test")
        result = await engine.enrich_with_consensus(
            domain="company",
            input_payload={"entity_name": "Acme", "company_domain": "acme.test"},
            max_variations=2,
            max_concurrent=2,
        )

        # Positive: the consensus path ran and merged the real field.
        assert "error:no_perplexity_source" not in result.flags
        assert result.variations_valid >= 1
        assert result.fields.get("company_industry") == "Plastics Recycling"

        # Negative: the wrapper keys never appear as enriched fields.
        assert "fields" not in result.fields
        assert "confidence" not in result.fields

    @pytest.mark.asyncio
    async def test_consensus_import_block_executes(self) -> None:
        """Guards the function-local imports in enrich_with_consensus.

        A wrong relative level here raises ModuleNotFoundError only when the
        method runs. Importing the names the same way the function does proves
        they resolve.
        """
        from app.services.prompt_builder import (  # noqa: F401
            build_variation_prompts,
            unwrap_envelope,
        )
