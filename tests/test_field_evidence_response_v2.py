"""Converge response v2: per-field evidence states in the output path (Campaign B, B2).

Checked against docs/contracts/converge/response.v2.md (frozen 2026-11-13). The
field names are neutral: nothing here depends on a domain.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.engines.converge_contract import restrict_fields_to_schema
from app.engines.convergence.convergence_config import ConvergenceConfig
from app.engines.convergence.field_evidence import (
    RESPONSE_CONTRACT_V2,
    EvidenceContractError,
    build_field_evidence,
    build_proposals,
    check_response_v2,
    merge_observation_evidence,
    withheld_fields,
)
from app.engines.convergence_controller import run_convergence_loop
from app.engines.enrichment_orchestrator import enrich_entity
from app.models.schemas import EnrichRequest, EnrichResponse
from app.services.perplexity_client import SonarResponse

IDENTITY = {"subject_ref": "rec:1", "subject_type": "site", "role": "seller"}
V2_KEYS = {"contract_version", "field_evidence", "proposals"}


def _record(value: Any, confidence: float, source: str = "consensus") -> dict[str, Any]:
    return {"value": value, "confidence": confidence, "source": source, "pass_number": 1}


def _build(**overrides: Any) -> dict[str, dict[str, Any]]:
    args: dict[str, Any] = {
        "requested": [],
        "fields": {},
        "produced": {},
        "provenance": {},
        "withheld": {},
        "entity": {},
        "identity": IDENTITY,
        "unknown_reason": "no_evidence",
    }
    return build_field_evidence(**{**args, **overrides})


# ── withheld_fields: what the replies said about fields the pass did not admit ──


def test_withheld_keeps_one_candidate_per_distinct_value_with_its_strongest_confidence() -> None:
    replies = [
        {"confidence": 0.4, "zip": "10001", "city": "Austin", "floors": 3},
        {"confidence": 0.7, "zip": "10002", "floors": 3.0, "note": "unsolicited", "phone": ""},
        {"confidence": 0.9, "zip": "10001"},
        {"confidence": "not a number", "zip": "10003"},
    ]
    withheld = withheld_fields(
        replies, admitted=["city"], requested=["zip", "city", "floors", "phone"]
    )
    assert set(withheld) == {"zip", "floors"}  # admitted, unsolicited and empty are absent
    assert withheld["zip"]["candidates"] == [
        {"value": "10001", "confidence": 0.9},
        {"value": "10002", "confidence": 0.7},
        {"value": "10003", "confidence": 0.0},  # unusable confidence is never defaulted upward
    ]
    assert withheld["floors"]["candidates"] == [{"value": 3.0, "confidence": 0.7}]  # 3 == 3.0


# ── build_field_evidence: one entry per state ──────────────────────────────────


def test_each_evidence_state_is_stated_and_unknown_and_conflicting_carry_no_value() -> None:
    evidence = _build(
        requested=["city", "tier", "zip", "phone", "name", "site_url", "region"],
        fields={"city": "Austin", "tier": "t1", "region": "south"},
        produced={
            "city": _record("Austin", 0.9),
            "tier": _record("t1", 0.8, "inference"),
            "region": _record("south", 0.6, "graph_inference"),
        },
        provenance={
            "tier": {"rule_id": "tier-rule", "rule_version": "v1", "inputs": {"city": "Austin"}}
        },
        withheld={
            "zip": {
                "candidates": [{"value": "1", "confidence": 0.7}, {"value": "2", "confidence": 0.6}]
            },
            "phone": {"candidates": [{"value": "555", "confidence": 0.5}], "pass_number": 2},
        },
        entity={"name": "Acme", "city": "ignored: the loop produced a value"},
    )
    assert {n: e["state"] for n, e in evidence.items()} == {
        "city": "external_claim",
        "tier": "inferred",
        "zip": "conflicting",
        "phone": "external_claim",
        "name": "observed",
        "site_url": "unknown",
        "region": "external_claim",
    }
    assert {n for n, e in evidence.items() if e["admitted"]} == {"city", "tier", "region"}
    for name in ("zip", "site_url"):
        assert "value" not in evidence[name] and "unit_status" not in evidence[name]
        assert evidence[name]["confidence"] is None and evidence[name]["admitted"] is False
    assert evidence["site_url"]["unknown_reason"] == "no_evidence"
    assert evidence["zip"]["conflict"]["resolution"] is None
    assert [c["value"] for c in evidence["zip"]["conflict"]["candidates"]] == ["1", "2"]
    assert evidence["phone"]["value"] == "555" and evidence["phone"]["confidence"] == 0.5
    assert evidence["phone"]["sources"][0]["source_ref"] == "pass:2/replies"
    assert evidence["tier"]["derivation"] == {
        "method": "tier-rule",
        "rule_or_model_version": "v1",
        "inputs": ["city"],
        "kb_fragment_ids": [],
    }
    assert evidence["region"]["sources"][0]["source_type"] == "graph"
    # The consumer's own value: evidence only, never scored, never a result.
    assert evidence["name"]["confidence"] is None and evidence["name"]["admitted"] is False
    assert evidence["name"]["sources"][0]["source_type"] == "request_entity"
    for entry in evidence.values():
        assert entry["identity"] == IDENTITY
        assert entry.get("unit_status", "unknown") == "unknown"  # no unit is recorded: not guessed
        assert all(source["independent"] is None for source in entry["sources"])
        assert (entry["unknown_reason"] is not None) == (entry["state"] == "unknown")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"produced": {"x": _record(1, 0.9, "inference")}},
            "inferred without a rule id and version",
        ),
        (
            {
                "produced": {"x": _record(1, 0.9, "inference")},
                "provenance": {"x": {"rule_id": "r"}},
            },
            "inferred without a rule id and version",
        ),
        ({"produced": {"x": _record(1, 0.9, "hearsay")}}, "unrecognized value source"),
        ({"fields": {"x": 1}}, "in fields with no recorded source"),
        ({"withheld": {"x": {"candidates": []}}}, "withheld with no candidate"),
    ],
)
def test_a_record_that_cannot_be_stated_is_refused_not_repaired(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(EvidenceContractError, match=message):
        _build(requested=["x"], **overrides)


def test_check_response_v2_enforces_the_producer_rules() -> None:
    evidence = _build(
        requested=["city", "zip"], fields={"city": "A"}, produced={"city": _record("A", 0.9)}
    )
    body = {
        "state": "completed",
        "fields": {"city": "A"},
        "field_evidence": evidence,
        "proposals": [],
    }
    check_response_v2(body, ["city", "zip"])
    cases = [
        (
            {**body, "fields": {"city": "A", "zip": "1"}},
            ["city", "zip"],
            "not exactly the admitted",
        ),
        ({**body, "fields": {}}, ["city", "zip"], "not exactly the admitted"),
        (body, ["city", "zip", "phone"], "without an entry"),
        ({**body, "state": "failed"}, ["city"], "a failed response admits nothing"),
        ({**body, "state": "degraded", "proposals": [{}]}, ["city"], "only a completed response"),
    ]
    for candidate, requested, message in cases:
        with pytest.raises(EvidenceContractError, match=message):
            check_response_v2(candidate, requested)
    coerced = {**evidence, "zip": {**evidence["zip"], "value": "1"}}
    with pytest.raises(EvidenceContractError, match="value presence"):
        check_response_v2({**body, "field_evidence": coerced}, ["city"])


def test_proposal_points_at_entries_and_lists_why_review_is_required() -> None:
    evidence = _build(
        requested=["city", "zip", "site_url"],
        fields={"city": "A"},
        produced={"city": _record("A", 0.9)},
        withheld={"zip": {"candidates": [{"value": "1", "confidence": 0.4}]}},
    )
    (proposal,) = build_proposals(evidence, IDENTITY)
    assert proposal["proposal_type"] == "site" and proposal["identity"] == IDENTITY
    assert proposal["fields"] == ["city", "zip", "site_url"]
    assert proposal["admitted_fields"] == ["city"]
    assert proposal["review"] == {
        "required": True,
        "reasons": ["contains_external_claim", "contains_unknown", "contains_unadmitted"],
    }
    assert build_proposals(evidence, {**IDENTITY, "subject_type": None}) == []


# ── The response model: v1 stays v1 ────────────────────────────────────────────


def test_a_v1_response_serializes_without_any_v2_key() -> None:
    assert not V2_KEYS & set(EnrichResponse(fields={"a": 1}).model_dump())
    v2 = EnrichResponse(contract_version=RESPONSE_CONTRACT_V2, field_evidence={}, proposals=[])
    assert set(v2.model_dump()) >= V2_KEYS


# ── Through the loop ───────────────────────────────────────────────────────────


def _settings() -> MagicMock:
    settings = MagicMock()
    settings.perplexity_api_key = "test-key"
    settings.perplexity_model = "sonar"
    settings.max_concurrent_variations = 3
    settings.default_timeout_seconds = 30
    settings.max_budget_tokens = 30000
    return settings


def _resolver() -> MagicMock:
    resolver = MagicMock()
    resolver.resolve = MagicMock(
        return_value={"context_text": "", "content_hash": "h", "fragment_ids": [], "kb_files": []}
    )
    return resolver


RULES = [
    {
        "rule_id": "tier-from-city",
        "conditions": [{"field": "city", "operator": "EQUALS", "value": "Austin"}],
        "outputs": [{"field": "tier", "value_expr": "t1", "derivation_type": "classification"}],
        "confidence": 0.9,
        "priority": 1,
        "rule_or_model_version": "v-test",
    }
]
SCHEMA = dict.fromkeys(("city", "tier", "zip", "phone", "name", "site_url"), "string")


async def _converge(pass_response: EnrichResponse, **loop: Any) -> EnrichResponse:
    async def enricher(pass_request, settings, kb_resolver, idem_store, sonar_config=None):
        return pass_response

    request = EnrichRequest(
        entity={"id": "rec:1", "name": "Acme"},
        object_type="Account",
        objective="Fill the record",
        schema=SCHEMA,
        max_variations=1,
    )
    return await run_convergence_loop(
        request=request,
        settings=_settings(),
        kb_resolver=_resolver(),
        enricher=enricher,
        inference_rules=RULES,
        convergence_config=ConvergenceConfig(max_passes=1),
        **loop,
    )


def _pass(state: str = "completed", **extra: Any) -> EnrichResponse:
    return EnrichResponse(
        fields={"city": "Austin"},
        confidence=0.9,
        tokens_used=10,
        state=state,
        feature_vector={
            "per_field_confidence": {"city": 0.9},
            "withheld_fields": {
                "zip": {
                    "candidates": [
                        {"value": "1", "confidence": 0.7},
                        {"value": "2", "confidence": 0.6},
                    ]
                },
                "phone": {"candidates": [{"value": "555", "confidence": 0.5}]},
            },
        },
        **extra,
    )


@pytest.mark.asyncio
async def test_v2_loop_response_carries_a_complete_per_field_evidence_state() -> None:
    response = await _converge(
        _pass(), response_contract=RESPONSE_CONTRACT_V2, subject={"type": "site", "role": "seller"}
    )
    body = response.model_dump()
    assert body["contract_version"] == RESPONSE_CONTRACT_V2 and body["state"] == "completed"
    evidence = body["field_evidence"]
    assert set(evidence) == set(SCHEMA)  # every requested field, none missing
    assert {n: e["state"] for n, e in evidence.items()} == {
        "city": "external_claim",
        "tier": "inferred",
        "zip": "conflicting",
        "phone": "external_claim",
        "name": "observed",
        "site_url": "unknown",
    }
    assert body["fields"] == {"city": "Austin", "tier": "t1"}
    assert body["fields"] == {n: e["value"] for n, e in evidence.items() if e["admitted"]}
    assert evidence["city"]["confidence"] == pytest.approx(0.9)
    assert evidence["tier"]["derivation"]["method"] == "tier-from-city"
    assert evidence["tier"]["derivation"]["rule_or_model_version"] == "v-test"
    assert evidence["tier"]["derivation"]["inputs"] == ["city"]
    assert evidence["city"]["identity"] == {
        "subject_ref": "rec:1",
        "subject_type": "site",
        "role": "seller",
    }
    (proposal,) = body["proposals"]
    assert proposal["proposal_type"] == "site" and proposal["admitted_fields"] == ["city", "tier"]
    check_response_v2(body, list(SCHEMA))


@pytest.mark.asyncio
async def test_v1_answer_is_unchanged_and_an_unknown_contract_is_refused() -> None:
    v1 = (await _converge(_pass())).model_dump()
    assert not V2_KEYS & set(v1) and v1["fields"] == {"city": "Austin", "tier": "t1"}
    with pytest.raises(ValueError, match="unsupported_contract"):
        await _converge(_pass(), response_contract="eie.converge.response/v3")


@pytest.mark.asyncio
async def test_v2_failed_run_admits_nothing_proposes_nothing_and_names_why_fields_are_unknown() -> (
    None
):
    failed = _pass(state="failed", failure_reason="no_valid_responses")
    body = (await _converge(failed, response_contract=RESPONSE_CONTRACT_V2)).model_dump()
    assert body["state"] == "failed" and body["fields"] == {} and body["proposals"] == []
    assert not any(e["admitted"] for e in body["field_evidence"].values())
    assert body["field_evidence"]["site_url"]["unknown_reason"] == "source_unavailable"
    assert body["field_evidence"]["city"]["state"] == "unknown"  # a failed pass is not evidence
    assert (
        body["field_evidence"]["city"]["identity"]["subject_type"] is None
    )  # no subject: not derived


@pytest.mark.asyncio
async def test_v2_refuses_itself_when_the_record_cannot_be_stated() -> None:
    async def enricher(pass_request, settings, kb_resolver, idem_store, sonar_config=None):
        return _pass()

    request = EnrichRequest(
        entity={"name": "Acme"}, object_type="Account", objective="x", schema=SCHEMA
    )
    response = await run_convergence_loop(
        request=request,
        settings=_settings(),
        kb_resolver=_resolver(),
        enricher=enricher,
        inference_rules=[],
        convergence_config=ConvergenceConfig(max_passes=1),
        response_contract=RESPONSE_CONTRACT_V2,
    )
    body = response.model_dump()
    assert body["state"] == "failed" and body["fields"] == {}
    assert body["failure_reason"].startswith("evidence_contract_violation: ")
    assert not V2_KEYS & set(body)  # never a partial v2 body


# ── The single pass reports what it withheld only when asked ───────────────────


@pytest.mark.asyncio
async def test_single_pass_reports_withheld_fields_only_on_request() -> None:
    def reply(confidence: float, **fields: Any) -> SonarResponse:
        return SonarResponse(
            data={"confidence": confidence, **fields}, tokens_used=10, citations=[], model="sonar"
        )

    request = EnrichRequest(
        entity={"name": "Acme"},
        object_type="Account",
        objective="Find the address",
        schema={"city": "string", "zip": "string"},
        field_thresholds={"city": 0.5, "zip": 0.95},
        max_variations=2,
    )
    replies = [reply(0.9, city="Austin", zip="10001"), reply(0.9, city="Austin", zip="10002")]

    async def run(**kwargs: Any) -> EnrichResponse:
        with patch(
            "app.engines.enrichment_orchestrator.query_perplexity",
            new_callable=AsyncMock,
            side_effect=[*replies, *replies],
        ):
            return await enrich_entity(request, _settings(), _resolver(), **kwargs)

    plain = await run()
    assert plain.fields == {"city": "Austin"}
    assert "withheld_fields" not in (plain.feature_vector or {})
    asked = await run(report_withheld=True)
    assert asked.fields == {"city": "Austin"}
    assert asked.feature_vector["withheld_fields"] == {
        "zip": {
            "candidates": [
                {"value": "10001", "confidence": 0.9},
                {"value": "10002", "confidence": 0.9},
            ]
        }
    }


# ── Codex review of the integration wiring (#347) ──


@pytest.mark.asyncio
async def test_v2_fields_outside_the_schema_are_cut_before_evidence_is_stated() -> None:
    """fields == admitted entries survives contract v1's cut to the request schema.

    The cut used to run in the handler after the v2 body was built, so a field
    outside the schema left `fields` while its entry stayed admitted.
    """
    extra = _pass()
    extra.fields["website"] = "acme.example"
    extra.feature_vector["per_field_confidence"]["website"] = 0.9
    body = (
        await _converge(
            extra,
            response_contract=RESPONSE_CONTRACT_V2,
            subject={"type": "site", "role": "seller"},
        )
    ).model_dump()

    assert "website" not in body["fields"]
    assert not body["field_evidence"].get("website", {}).get("admitted")
    assert restrict_fields_to_schema(body, SCHEMA) == []  # nothing left for the handler to cut
    assert body["fields"] == {
        n: e["value"] for n, e in body["field_evidence"].items() if e["admitted"]
    }
    check_response_v2(body, list(SCHEMA))


def _observed(value: Any) -> dict[str, dict[str, Any]]:
    entry = {
        "state": "observed",
        "admitted": False,
        "value": value,
        "unit_status": "unknown",
        "confidence": None,
        "sources": [{"source_type": "request_observation", "source_ref": "o:1"}],
        "derivation": None,
        "conflict": None,
        "unknown_reason": None,
        "identity": IDENTITY,
    }
    return {"flag": entry}


@pytest.mark.parametrize(
    ("produced", "observed", "state"),
    [(1, True, "conflicting"), (0, False, "conflicting"), (3, 3.0, "external_claim")],
)
def test_observation_agreement_uses_value_identity_not_raw_equality(
    produced: Any, observed: Any, state: str
) -> None:
    run = _build(
        requested=["flag"], fields={"flag": produced}, produced={"flag": _record(produced, 0.9)}
    )
    merged = merge_observation_evidence(run, _observed(observed))["flag"]
    assert merged["state"] == state
    assert merged["admitted"] is (state == "external_claim")
