"""
End-to-end convergence loop integration test.

Exercises the full convergence path: request → convergence_controller →
enricher (mocked LLM) → inference bridge → field confidence →
cost tracking → telemetry → loop state → response.

Only the LLM/network boundary is mocked via the `enricher` kwarg.
All internal wiring (MetaPromptPlanner, InferenceBridge, CostTracker,
ConfidenceTracker) runs for real.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from app.engines.convergence_controller import run_convergence_loop
from app.models.schemas import EnrichRequest, EnrichResponse

pytestmark = pytest.mark.integration


def _make_enricher(pass_responses: list[dict[str, Any]]):
    """Build a callable enricher that returns different fields per pass."""
    call_count = 0

    async def _enricher(
        request: EnrichRequest,
        settings: Any,
        kb_resolver: Any = None,
        idem_store: Any = None,
        sonar_config: Any = None,
    ) -> EnrichResponse:
        nonlocal call_count
        idx = min(call_count, len(pass_responses) - 1)
        call_count += 1
        resp = pass_responses[idx]

        return EnrichResponse(
            fields=resp.get("fields", {}),
            confidence=resp.get("confidence", 0.8),
            uncertainty_score=resp.get("uncertainty", 2.5),
            tokens_used=resp.get("tokens", 1000),
            processing_time_ms=100,
            state="completed",
            feature_vector=resp.get("feature_vector"),
        )

    return _enricher


@pytest.fixture
def settings() -> MagicMock:
    s = MagicMock()
    s.default_domain = "plastics_recycling"
    s.perplexity_api_key = "test-key"
    s.convergence_threshold = 2.0
    s.max_convergence_passes = 5
    s.max_budget_tokens = 50000
    s.sonar_token_rate_per_1k = 0.005
    return s


@pytest.fixture
def enrich_request() -> EnrichRequest:
    return EnrichRequest(
        entity={"Name": "Test Recycler", "polymer_type": "HDPE"},
        object_type="Account",
        objective="Enrich plastics recycling data",
    )


class TestConvergenceLoopE2E:
    @pytest.mark.asyncio
    async def test_converges_within_passes(
        self, settings: MagicMock, enrich_request: EnrichRequest
    ) -> None:
        enricher = _make_enricher(
            [
                {
                    "fields": {"polymer_type": "HDPE", "mfi_range": "0.5-3.0"},
                    "confidence": 0.75,
                    "uncertainty": 2.8,
                    "tokens": 1500,
                },
                {
                    "fields": {
                        "polymer_type": "HDPE",
                        "mfi_range": "0.5-3.0",
                        "material_grade": "B+",
                    },
                    "confidence": 0.88,
                    "uncertainty": 1.5,
                    "tokens": 1000,
                },
                {
                    "fields": {
                        "polymer_type": "HDPE",
                        "mfi_range": "0.5-3.0",
                        "material_grade": "B+",
                        "contamination_pct": 3.2,
                    },
                    "confidence": 0.92,
                    "uncertainty": 0.8,
                    "tokens": 800,
                },
            ]
        )

        result = await run_convergence_loop(
            request=enrich_request,
            settings=settings,
            kb_resolver=None,
            idem_store=None,
            enricher=enricher,
        )

        assert result.state == "completed"
        assert result.confidence > 0
        assert result.tokens_used > 0
        assert result.pass_count >= 1

    @pytest.mark.asyncio
    async def test_budget_exhaustion_stops_loop(
        self, settings: MagicMock, enrich_request: EnrichRequest
    ) -> None:
        settings.max_budget_tokens = 2000

        enricher = _make_enricher(
            [
                {
                    "fields": {"polymer_type": "HDPE"},
                    "confidence": 0.6,
                    "uncertainty": 3.0,
                    "tokens": 1500,
                },
                {
                    "fields": {"polymer_type": "HDPE"},
                    "confidence": 0.65,
                    "uncertainty": 2.8,
                    "tokens": 1500,
                },
            ]
        )

        result = await run_convergence_loop(
            request=enrich_request,
            settings=settings,
            kb_resolver=None,
            idem_store=None,
            enricher=enricher,
        )

        assert result.tokens_used > 0
        assert result.state == "completed"

    @pytest.mark.asyncio
    async def test_max_passes_respected(
        self, settings: MagicMock, enrich_request: EnrichRequest
    ) -> None:
        from app.engines.convergence.convergence_config import ConvergenceConfig

        config = ConvergenceConfig(max_passes=2)

        enricher = _make_enricher(
            [
                {"fields": {"f1": "v1"}, "confidence": 0.5, "uncertainty": 4.0, "tokens": 500},
                {
                    "fields": {"f1": "v1", "f2": "v2"},
                    "confidence": 0.6,
                    "uncertainty": 3.5,
                    "tokens": 500,
                },
                {
                    "fields": {"f1": "v1", "f2": "v2", "f3": "v3"},
                    "confidence": 0.7,
                    "uncertainty": 3.0,
                    "tokens": 500,
                },
            ]
        )

        result = await run_convergence_loop(
            request=enrich_request,
            settings=settings,
            kb_resolver=None,
            idem_store=None,
            enricher=enricher,
            convergence_config=config,
        )

        assert result.state == "completed"
        assert result.pass_count <= 2

    @pytest.mark.asyncio
    async def test_enricher_failure_produces_response(
        self, settings: MagicMock, enrich_request: EnrichRequest
    ) -> None:
        async def failing_enricher(*args: Any, **kwargs: Any) -> EnrichResponse:
            return EnrichResponse(
                fields={},
                confidence=0.0,
                uncertainty_score=5.0,
                tokens_used=0,
                processing_time_ms=0,
                state="failed",
            )

        result = await run_convergence_loop(
            request=enrich_request,
            settings=settings,
            kb_resolver=None,
            idem_store=None,
            enricher=failing_enricher,
        )

        assert result.state in ("failed", "completed")


# ── Live inference through the SDK converge action ──────────────────────────
#
# Everything from the TransportPacket to the response runs for real: SDK
# dispatch, handle_converge, domain context loading from domains/plasticos,
# run_convergence_loop and InferenceBridge executing the domain's own
# `inference_rules`. Only the provider boundary (enrich_entity) and the
# post-enrich side effects (persistence store, Gate router, event emitter) are
# replaced. The rules exercised are the production PlasticOS rules:
#   grade-assignment-hdpe-a  polymer_types CONTAINS HDPE, contamination < 0.03
#                            -> material_grade = "A"            (0.92)
#   tier-1-high-capacity     annual_capacity_mt >= 5000, material_grade IN [A, A+]
#                            -> facility_tier = "tier_1"        (0.90)

_ENTITY_REF = "res.partner:55"
_REPO_DOMAINS = Path(__file__).resolve().parents[2] / "domains"


def _evidence(**overrides: Any) -> dict[str, tuple[Any, float]]:
    """Provider evidence for one pass: field -> (value, per-field confidence)."""
    evidence: dict[str, tuple[Any, float]] = {
        "polymer_types": (["HDPE", "PP"], 0.95),
        "contamination_tolerance": (0.02, 0.90),
        "annual_capacity_mt": (6000.0, 0.90),
    }
    for name, value in overrides.items():
        if value is None:
            evidence.pop(name, None)
        else:
            evidence[name] = value
    return evidence


def _provider(
    evidence: dict[str, tuple[Any, float]] | list[dict[str, tuple[Any, float]]],
) -> AsyncMock:
    """Stand-in for enrich_entity: the LLM/network boundary, nothing else.

    A list is per-pass evidence; the last entry answers every later pass.
    """
    passes = evidence if isinstance(evidence, list) else [evidence]
    calls = {"n": 0}

    async def _enrich(request, settings, kb_resolver=None, idem_store=None, **_kw):
        evidence = passes[min(calls["n"], len(passes) - 1)]
        calls["n"] += 1
        return EnrichResponse(
            fields={k: v for k, (v, _c) in evidence.items()},
            confidence=0.9,
            uncertainty_score=1.0,
            tokens_used=400,
            processing_time_ms=5,
            state="completed",
            feature_vector={"per_field_confidence": {k: c for k, (_v, c) in evidence.items()}},
        )

    return AsyncMock(side_effect=_enrich)


def _canonical_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "entity": {"name": "Acme Recycling", "id": _ENTITY_REF, "_odoo_entity_id": _ENTITY_REF},
        "object_type": "plasticos",
        "objective": "Full entity enrichment and inference",
        "max_variations": 3,
        "consensus_threshold": 0.80,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def sdk_converge():
    """Register the real handlers with a real DomainYamlReader; dispatch via the SDK."""
    from constellation_node_sdk.runtime.execution import execute_transport_packet
    from constellation_node_sdk.runtime.handlers import clear_handlers
    from constellation_node_sdk.transport import create_transport_packet

    from app.engines import orchestration_layer
    from app.engines.domain_yaml_reader import DomainYamlReader
    from app.services.side_effect_coordinator import get_side_effect_coordinator

    state: dict[str, Any] = {"evidence": _evidence(), "domains": _REPO_DOMAINS}

    with (
        patch("app.services.result_store.ResultStore.persist_enrich_response") as persist,
        patch("app.engines.packet_router.get_router") as router_factory,
        patch("app.services.event_emitter.get_emitter") as emitter_factory,
    ):
        persist.return_value = None
        state["persist"] = persist  # the persistence sink, for tests that assert on it
        router = router_factory.return_value
        router.notify_graph_sync = AsyncMock(return_value=None)
        router.notify_score_invalidate = AsyncMock(return_value=None)
        emitter_factory.return_value.emit_enrichment_completed = AsyncMock(return_value=None)
        get_side_effect_coordinator().reset_for_tests()

        async def run(payload: dict[str, Any], tenant: str = "tenant-a") -> tuple[dict, AsyncMock]:
            clear_handlers()
            orchestration_layer.register(
                kb=None, idem_store=None, domain_reader=DomainYamlReader(state["domains"])
            )
            provider = _provider(state["evidence"])
            packet = create_transport_packet(
                action="converge",
                payload=payload,
                tenant=tenant,
                source_node="gate",
                destination_node="enrichment-engine",
                reply_to="gate",
            )
            with patch("app.engines.enrichment_orchestrator.enrich_entity", provider):
                result = await execute_transport_packet(
                    packet,
                    node_name="enrichment-engine",
                    dev_mode=True,
                    allowed_actions=("converge",),
                )
            return dict(result.payload), provider

        yield state, run, router
        get_side_effect_coordinator().reset_for_tests()
        clear_handlers()


def _inferred(response: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Merge per-pass inference provenance into one field -> record map."""
    merged: dict[str, dict[str, Any]] = {}
    for entry in response.get("inferences") or []:
        merged.update(entry.get("inference_provenance") or {})
    return merged


@pytest.mark.asyncio
class TestLiveDomainInference:
    async def test_multi_hop_derivation_through_sdk_converge(self, sdk_converge) -> None:
        _state, run, router = sdk_converge
        response, provider = await run(_canonical_payload())

        assert response["state"] == "completed"
        assert provider.await_count == 1  # no extra provider pass
        assert response["fields"]["material_grade"] == "A"
        assert response["fields"]["facility_tier"] == "tier_1"

        provenance = _inferred(response)
        grade = provenance["material_grade"]
        assert grade["rule_id"] == "grade-assignment-hdpe-a"
        assert grade["rule_version"] == "plasticos-inference-0.3.0"
        assert grade["cascade_level"] == 0
        assert grade["inputs"] == {"polymer_types": ["HDPE", "PP"], "contamination_tolerance": 0.02}
        # Bounded by the weakest evidence it consumed (0.90), never the rule's 0.92.
        assert grade["confidence"] == pytest.approx(0.90)

        tier = provenance["facility_tier"]
        assert tier["rule_id"] == "tier-1-high-capacity"
        assert tier["cascade_level"] == 1
        assert tier["inputs"]["material_grade"] == "A"  # consumes the first derivation
        assert tier["input_confidence"]["material_grade"] == pytest.approx(0.90)
        assert tier["confidence"] == pytest.approx(0.90)

        tracked = response["feature_vector"]["confidence_tracking"]["fields"]
        assert tracked["material_grade"]["confidence"] == pytest.approx(0.90)
        assert response["feature_vector"]["inference"]["status"] == "active"
        router.notify_graph_sync.assert_not_awaited()  # E14: no Graph call on this path

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"contamination_tolerance": None}, id="missing-input"),
            pytest.param({"contamination_tolerance": (0.12, 0.9)}, id="unmet-condition"),
            pytest.param({"polymer_types": (["PET"], 0.95)}, id="non-matching-polymer"),
        ],
    )
    async def test_unsupported_derivation_does_not_fire(self, sdk_converge, overrides) -> None:
        state, run, _router = sdk_converge
        state["evidence"] = _evidence(**overrides)
        response, _provider_mock = await run(_canonical_payload())

        assert response["state"] == "completed"
        assert "material_grade" not in response["fields"]
        assert "facility_tier" not in response["fields"]  # second hop needs the first
        assert _inferred(response) == {}

    async def test_existing_fact_is_never_overwritten(self, sdk_converge) -> None:
        _state, run, _router = sdk_converge
        payload = _canonical_payload()
        payload["entity"]["material_grade"] = "C"  # the record's own value
        response, _provider_mock = await run(payload)

        provenance = _inferred(response)
        assert "material_grade" not in provenance
        assert "material_grade" not in response["fields"]
        # The record's "C" is what the second hop sees, so tier-1 cannot fire.
        assert "facility_tier" not in provenance

    async def test_low_confidence_evidence_bounds_derived_confidence(self, sdk_converge) -> None:
        state, run, _router = sdk_converge
        state["evidence"] = _evidence(polymer_types=(["HDPE"], 0.40))
        response, _provider_mock = await run(_canonical_payload(max_passes=1))

        provenance = _inferred(response)
        assert provenance["material_grade"]["confidence"] == pytest.approx(0.40)
        assert provenance["facility_tier"]["confidence"] <= 0.40

    async def test_missing_domain_runs_without_inference(self, sdk_converge) -> None:
        _state, run, _router = sdk_converge
        response, _provider_mock = await run(_canonical_payload(object_type="no_such_domain"))

        assert response["state"] == "completed"
        assert _inferred(response) == {}
        assert response["feature_vector"]["inference"]["status"] == "not_configured"
        assert "material_grade" not in response["fields"]

    async def test_incompatible_rules_are_reported_not_executed(
        self, sdk_converge, tmp_path: Path
    ) -> None:
        state, run, _router = sdk_converge
        spec = yaml.safe_load((_REPO_DOMAINS / "plasticos" / "spec.yaml").read_text())
        spec["inference_rules"][0]["conditions"][0]["operator"] = "MATCHES"
        (tmp_path / "plasticos").mkdir()
        (tmp_path / "plasticos" / "spec.yaml").write_text(yaml.safe_dump(spec))
        state["domains"] = tmp_path

        response, _provider_mock = await run(_canonical_payload())

        assert response["state"] == "completed"  # enrichment itself is unaffected
        inference = response["feature_vector"]["inference"]
        assert inference["status"] == "invalid_rules"
        assert any("grade-assignment-hdpe-a" in err for err in inference["errors"])
        # The whole rule set is refused: no partial chain from the rules that parsed.
        assert _inferred(response) == {}
        assert "facility_tier" not in response["fields"]

    async def test_low_confidence_rule_does_not_buy_another_provider_pass(
        self, sdk_converge, tmp_path: Path
    ) -> None:
        state, run, _router = sdk_converge
        spec = yaml.safe_load((_REPO_DOMAINS / "plasticos" / "spec.yaml").read_text())
        spec["inference_rules"][2]["confidence"] = 0.50  # tier-1-high-capacity
        (tmp_path / "plasticos").mkdir()
        (tmp_path / "plasticos" / "spec.yaml").write_text(yaml.safe_dump(spec))
        state["domains"] = tmp_path

        response, provider = await run(_canonical_payload())

        assert _inferred(response)["facility_tier"]["confidence"] == pytest.approx(0.50)
        # Research evidence already clears the bar; a rule-bounded 0.50 cannot
        # be raised by researching again, so it must not trigger pass 2.
        assert provider.await_count == 1
        assert response["pass_count"] == 1
        # The serialized flag agrees with the decision that stopped the loop.
        assert response["feature_vector"]["confidence_tracking"]["converged"] is True

    @pytest.mark.parametrize(
        "polymer_types",
        [pytest.param(5, id="numeric"), pytest.param([{"name": "HDPE"}], id="list-of-dict")],
    )
    async def test_malformed_provider_value_fails_the_rule_not_the_converge(
        self, sdk_converge, polymer_types: Any
    ) -> None:
        """Provider values are untrusted shapes; a rule that cannot read one is False.

        On the pre-fix head both shapes raised TypeError out of the SDK handler
        with nothing persisted, where main completed and persisted the same answer.
        """
        state, run, _router = sdk_converge
        state["evidence"] = _evidence(polymer_types=(polymer_types, 0.95))
        response, _provider_mock = await run(_canonical_payload())

        assert response["state"] == "completed"
        assert state["persist"].call_count == 1
        assert response["fields"]["polymer_types"] == polymer_types  # the evidence is kept
        assert "material_grade" not in response["fields"]
        assert "facility_tier" not in response["fields"]
        assert _inferred(response) == {}
        inference = response["feature_vector"]["inference"]
        assert inference["status"] == "active"  # the rule set is fine; this value is not
        assert inference["errors"] == []

    async def test_rule_execution_fault_is_reported_not_raised(self, sdk_converge) -> None:
        """A fault inside rule execution degrades the inference, never the converge."""
        state, run, _router = sdk_converge
        with patch(
            "app.engines.inference_bridge_adapter.infer",
            side_effect=RuntimeError("operator exploded"),
        ):
            response, _provider_mock = await run(_canonical_payload())

        assert response["state"] == "completed"
        assert state["persist"].call_count == 1
        assert _inferred(response) == {}
        inference = response["feature_vector"]["inference"]
        assert inference["status"] == "active"  # the rules parsed; a run over values failed
        assert inference["errors"] == [
            "condition rules did not execute: RuntimeError: operator exploded"
        ]

    async def test_input_crossing_a_rule_boundary_is_rederived_on_the_next_pass(
        self, sdk_converge
    ) -> None:
        """A derivation follows its inputs: pass 2 evidence replaces pass 1's grade.

        Pass 1 researches contamination at 0.02 (weak, 0.40) -> grade A, tier 1.
        Pass 2 corrects it to 0.05 (0.95): grade-assignment-hdpe-b applies, and
        tier-1-high-capacity (A/A+ only) no longer does. On the pre-fix head the
        pass-1 derivations were treated as facts on pass 2 and never re-run, so
        the response carried contamination 0.05 beside grade A and tier 1 with
        provenance inputs frozen at 0.02.
        """
        state, run, _router = sdk_converge
        state["evidence"] = [
            _evidence(contamination_tolerance=(0.02, 0.40)),
            _evidence(contamination_tolerance=(0.05, 0.95)),
        ]
        response, provider = await run(_canonical_payload())

        assert response["state"] == "completed"
        assert provider.await_count == 2
        assert response["fields"]["contamination_tolerance"] == 0.05
        assert response["fields"]["material_grade"] == "B"
        assert "facility_tier" not in response["fields"]  # retracted: B is not A/A+

        per_pass = {entry["pass"]: entry for entry in response["inferences"]}
        pass1 = per_pass[1]["inference_provenance"]
        assert pass1["material_grade"]["rule_id"] == "grade-assignment-hdpe-a"
        assert pass1["material_grade"]["inputs"]["contamination_tolerance"] == 0.02
        assert "facility_tier" in pass1
        pass2 = per_pass[2]["inference_provenance"]
        assert set(pass2) == {"material_grade"}
        assert pass2["material_grade"]["rule_id"] == "grade-assignment-hdpe-b"
        assert pass2["material_grade"]["inputs"]["contamination_tolerance"] == 0.05
        # Re-bounded by the inputs now in the response: min(rule 0.85, 0.95, 0.95).
        assert pass2["material_grade"]["confidence"] == pytest.approx(0.85)

        tracked = response["feature_vector"]["confidence_tracking"]["fields"]
        assert tracked["material_grade"]["value"] == "B"
        assert tracked["material_grade"]["confidence"] == pytest.approx(0.85)
        assert "facility_tier" not in tracked

    async def test_provider_evidence_for_a_derived_field_becomes_the_fact(
        self, sdk_converge
    ) -> None:
        """Once the provider evidences material_grade, it is no longer a derivation."""
        state, run, _router = sdk_converge
        state["evidence"] = [
            _evidence(contamination_tolerance=(0.02, 0.40)),  # pass 1 derives A, tier 1
            _evidence(contamination_tolerance=(0.02, 0.95), material_grade=("C", 0.95)),
        ]
        response, provider = await run(_canonical_payload())

        assert provider.await_count == 2
        assert response["fields"]["material_grade"] == "C"  # the evidence, not the rule
        assert "facility_tier" not in response["fields"]  # C is not A/A+: retracted
        assert "material_grade" not in response["inferences"][1]["inference_provenance"]
        tracked = response["feature_vector"]["confidence_tracking"]["fields"]
        assert tracked["material_grade"]["value"] == "C"
        assert tracked["material_grade"]["confidence"] == pytest.approx(0.95)

    async def test_missing_referenced_rules_file_is_refused_not_silent(
        self, sdk_converge, tmp_path: Path
    ) -> None:
        """A declared rules file that cannot be read is `invalid_rules`, not `not_configured`."""
        state, run, _router = sdk_converge
        spec = yaml.safe_load((_REPO_DOMAINS / "plasticos" / "spec.yaml").read_text())
        spec.pop("inference_rules")
        spec["inference_rules_path"] = str(tmp_path / "no-such-rules.yaml")
        (tmp_path / "plasticos").mkdir()
        (tmp_path / "plasticos" / "spec.yaml").write_text(yaml.safe_dump(spec))
        state["domains"] = tmp_path

        response, _provider_mock = await run(_canonical_payload())

        assert response["state"] == "completed"  # enrichment itself is unaffected
        inference = response["feature_vector"]["inference"]
        assert inference["status"] == "invalid_rules"
        assert inference["errors"] and all(isinstance(err, str) for err in inference["errors"])
        assert _inferred(response) == {}
        assert "material_grade" not in response["fields"]

    async def test_rules_from_inference_rules_path_document(
        self, sdk_converge, tmp_path: Path
    ) -> None:
        """A referenced rules file has load_rules' shape: an `inference_rules` section."""
        state, run, _router = sdk_converge
        spec = yaml.safe_load((_REPO_DOMAINS / "plasticos" / "spec.yaml").read_text())
        rules_file = tmp_path / "rules.yaml"
        rules_file.write_text(yaml.safe_dump({"inference_rules": spec.pop("inference_rules")}))
        spec["inference_rules_path"] = str(rules_file)
        (tmp_path / "plasticos").mkdir()
        (tmp_path / "plasticos" / "spec.yaml").write_text(yaml.safe_dump(spec))
        state["domains"] = tmp_path

        response, _provider_mock = await run(_canonical_payload())

        assert response["feature_vector"]["inference"]["status"] == "active"
        assert _inferred(response)["facility_tier"]["rule_id"] == "tier-1-high-capacity"

    async def test_replay_is_deterministic(self, sdk_converge) -> None:
        _state, run, _router = sdk_converge
        first, _p1 = await run(_canonical_payload())
        second, _p2 = await run(_canonical_payload())

        assert first["fields"] == second["fields"]
        assert _inferred(first) == _inferred(second)
        assert [e["inferred"] for e in first["inferences"]] == [
            e["inferred"] for e in second["inferences"]
        ]

    async def test_compat_dialect_loads_inference_but_keeps_partner_allowlist(
        self, sdk_converge
    ) -> None:
        state, run, _router = sdk_converge
        state["evidence"] = {
            **_evidence(),
            **dict.fromkeys(
                ("name", "website", "city", "zip", "street", "street2", "email", "phone"),
                ("x", 0.95),
            ),
        }
        payload = {
            "entity_id": _ENTITY_REF,
            "domain": "plasticos",
            "entity_snapshot": {"name": "Acme Recycling"},
            "max_passes": 1,
        }
        response, _provider_mock = await run(payload)

        assert response["state"] == "completed"
        assert _inferred(response)["facility_tier"]["rule_id"] == "tier-1-high-capacity"
        # Odoo's partner contract is unchanged: derived plastics fields stay out.
        assert "material_grade" not in response["fields"]
        assert "facility_tier" not in response["fields"]
