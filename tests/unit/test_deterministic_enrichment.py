"""EIE-009 — `enrich` must be able to complete without a paid provider.

Before this, `handle_enrich` had no offline path: without provider egress it
returned `failed / no_valid_responses (APIConnectionError ...)`, and since
`_persist_and_sync` only runs on a completed enrichment, the EIE -> Gate -> CEG
direction could not be driven through its real trigger in CI. The Constellation
E2E worked around it by calling PacketRouter directly — proving the transport
while skipping the business chain it was built to prove.
"""

from __future__ import annotations

import pytest

from app.core.config import DETERMINISTIC_PROVIDER_ENVIRONMENTS, Settings
from app.services.deterministic_provider import (
    DETERMINISTIC_CONFIDENCE,
    DETERMINISTIC_VALUE_PREFIX,
    PROVIDER_NAME,
    build_deterministic_payload,
    query_deterministic,
)

ENTITY = {"id": "acct-42", "Name": "Northwind Polymers", "country": "PT"}
SCHEMA = {"polymer_type": "string", "annual_tonnage": "integer", "country": "string"}
# EIE-212-F002 counterexamples: the three declared types no prefix can ride on.
UNMARKABLE_SCHEMA = {
    "annual_tonnage": "integer",
    "contamination_pct": "float",
    "is_certified": "boolean",
}
TENANT_FACING_ENVIRONMENTS = ("staging", "prod", "production")


def test_same_input_always_yields_the_same_answer() -> None:
    first = build_deterministic_payload(ENTITY, SCHEMA)
    second = build_deterministic_payload(dict(ENTITY), dict(SCHEMA))
    assert first == second


def test_a_different_entity_yields_a_different_answer() -> None:
    other = build_deterministic_payload({**ENTITY, "id": "acct-43"}, SCHEMA)
    assert (
        other["fields"]["polymer_type"]
        != build_deterministic_payload(ENTITY, SCHEMA)["fields"]["polymer_type"]
    )


def test_values_the_entity_already_carries_are_echoed_not_invented() -> None:
    fields = build_deterministic_payload(ENTITY, SCHEMA)["fields"]
    assert fields["country"] == "PT"


def test_invented_values_are_marked_as_synthetic() -> None:
    """A synthesized value must never be mistakable for a researched one."""
    fields = build_deterministic_payload(ENTITY, SCHEMA)["fields"]
    assert fields["polymer_type"].startswith(DETERMINISTIC_VALUE_PREFIX)


def test_declared_types_are_honoured() -> None:
    fields = build_deterministic_payload(ENTITY, SCHEMA)["fields"]
    assert isinstance(fields["annual_tonnage"], int)
    assert isinstance(fields["polymer_type"], str)


def test_an_entity_without_an_identifier_is_still_stable() -> None:
    anonymous = {"sector": "packaging", "region": "iberia"}
    assert build_deterministic_payload(anonymous, SCHEMA) == build_deterministic_payload(
        dict(anonymous), SCHEMA
    )


@pytest.mark.asyncio
async def test_response_matches_the_provider_contract() -> None:
    """The orchestrator swaps one for the other, so the shape must be identical."""
    response = await query_deterministic(entity=ENTITY, target_schema=SCHEMA)
    assert response.tokens_used == 0
    assert response.data["confidence"] == DETERMINISTIC_CONFIDENCE
    assert set(response.data["fields"]) == set(SCHEMA)


@pytest.mark.asyncio
async def test_every_variation_agrees_so_consensus_holds() -> None:
    """Multi-variation passes rely on agreement; a stable source gives full agreement."""
    responses = [await query_deterministic(entity=ENTITY, target_schema=SCHEMA) for _ in range(3)]
    assert all(r.data == responses[0].data for r in responses)


def test_confidence_clears_the_default_consensus_threshold() -> None:
    assert Settings().default_consensus_threshold <= DETERMINISTIC_CONFIDENCE


def test_provider_defaults_to_the_live_source() -> None:
    """Deterministic enrichment is opt-in. Nothing may fall back to it."""
    assert Settings().l9_enrichment_provider == "perplexity"


def test_an_unknown_provider_is_refused() -> None:
    with pytest.raises(ValueError, match="L9_ENRICHMENT_PROVIDER"):
        Settings(l9_enrichment_provider="wishful-thinking")


# --------------------------------------------------------------------------
# EIE-212-F002: synthetic identity survives, or production refuses the config.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "expected_type"),
    [("annual_tonnage", int), ("contamination_pct", float), ("is_certified", bool)],
)
def test_numeric_and_boolean_synthetic_values_carry_no_marker(
    field: str, expected_type: type
) -> None:
    """The counterexample the audit found, pinned so nobody claims otherwise again.

    A type-correct int, float or bool cannot start with ``det:``. The value that
    reaches ``fields`` — and therefore persistence and the Gate -> CEG sync — is
    indistinguishable from a researched one. That is why the guard below exists.
    """
    value = build_deterministic_payload(ENTITY, UNMARKABLE_SCHEMA)["fields"][field]
    assert isinstance(value, expected_type)
    assert not isinstance(value, str), "a marked value would be a string, and this is not"


def test_payload_names_every_synthetic_field_including_the_unmarkable_ones() -> None:
    """Provider-level identity: the payload says which fields it invented."""
    payload = build_deterministic_payload(ENTITY, {**UNMARKABLE_SCHEMA, "country": "string"})
    assert payload["provider"] == PROVIDER_NAME
    assert payload["synthetic_fields"] == ["annual_tonnage", "contamination_pct", "is_certified"]
    assert "country" not in payload["synthetic_fields"]  # echoed, not invented


@pytest.mark.parametrize("environment", TENANT_FACING_ENVIRONMENTS)
def test_deterministic_provider_is_refused_where_tenants_are_served(environment: str) -> None:
    """Production mode rejects the configuration at startup, not by convention."""
    with pytest.raises(ValueError, match="refused for L9_ENVIRONMENT"):
        Settings(l9_enrichment_provider="deterministic", l9_environment=environment)


@pytest.mark.parametrize("environment", sorted(DETERMINISTIC_PROVIDER_ENVIRONMENTS))
def test_deterministic_provider_is_permitted_in_non_production(environment: str) -> None:
    settings = Settings(l9_enrichment_provider="deterministic", l9_environment=environment)
    assert settings.l9_enrichment_provider == "deterministic"


def test_the_permitted_environments_are_exactly_the_sdk_unsigned_ones() -> None:
    """The guard's allowlist must not drift from the runtime's own dev_mode set."""
    assert frozenset({"local", "dev", "test"}) == DETERMINISTIC_PROVIDER_ENVIRONMENTS


def test_live_provider_is_unaffected_by_the_environment_guard() -> None:
    for environment in TENANT_FACING_ENVIRONMENTS:
        assert Settings(l9_environment=environment).l9_enrichment_provider == "perplexity"


# --------------------------------------------------------------------------
# The point of the whole module: enrich completes, offline.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_entity_completes_offline_and_calls_no_provider() -> None:
    """`state="completed"` is what gates _persist_and_sync, and therefore the
    Gate -> CEG leg. Reaching it without provider egress is the whole finding."""
    from unittest.mock import MagicMock, patch

    from app.engines.enrichment_orchestrator import enrich_entity
    from app.models.schemas import EnrichRequest

    settings = Settings(
        l9_enrichment_provider="deterministic",
        l9_environment="test",
        perplexity_api_key="",  # no key, deliberately
    )
    kb_resolver = MagicMock()
    kb_resolver.resolve = MagicMock(
        return_value={
            "context_text": "HDPE: MFI 0.1-25 g/10min",
            "content_hash": "abc123",
            "fragment_ids": ["polymers.hdpe"],
            "kb_files": ["hdpe.yaml"],
        }
    )
    # `schema_` carries validation_alias="schema", so the alias is the way in.
    request = EnrichRequest.model_validate(
        {
            "entity": {"id": "acct-42", "Name": "Northwind Polymers"},
            "object_type": "Account",
            "objective": "Enrich plastics data",
            "schema": SCHEMA,
        }
    )

    with patch(
        "app.engines.enrichment_orchestrator.query_perplexity",
        side_effect=AssertionError("the paid provider must not be called"),
    ) as never_called:
        response = await enrich_entity(request, settings, kb_resolver, None)

    assert never_called.await_count == 0
    assert response.state == "completed", response.failure_reason
    assert response.fields, "a completed enrichment must carry fields"
    assert response.tokens_used == 0
