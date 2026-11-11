"""Runtime contract for `action="converge"`: EnrichRequest in, EnrichResponse out.

`test_converge_contract_fixtures` pins the *shapes* against the live Pydantic
models. This file pins the thing those fixtures cannot: that the payload a real
producer sends actually reaches the code path that speaks them.

That gap is not hypothetical. `handle_converge` carried a second branch — a
compatibility adapter for an older `entity_snapshot` dialect — whose
discriminator was widened until it claimed every live request. Both fixture
tests stayed green throughout, because neither one dispatches. The adapter
meanwhile dropped `entity["id"]`, replaced the caller's `objective` and
`object_type` with its own, reinterpreted `max_variations` as convergence
passes, and truncated the response `fields` to eight partner keys.

So the assertions here are about routing and fidelity, deliberately:

* the live payload selects the canonical branch, not the adapter;
* what reaches the convergence loop is what the caller sent;
* what comes back is `EnrichResponse.model_dump()` and nothing else.

Contract authority for the request shape is the live producer, IB-Odoo_19
`plasticos_gate/services/gate_builders.py::build_converge_request` (PR #163);
for the response, `plasticos_gate/services/gate_mappers.py::map_converge_response`,
which reads `state`/`failure_reason`/`fields` and derives its own status from
`state == "completed"`. Nothing here imports Odoo — the payload is a structural
copy, so this stays a contract test rather than a cross-repo runtime dependency.

L9_META:
  tier: 2
  domain: convergence
  authority: L9 Master Kernel v3.0
  pr_class: app_code + tier2_test
"""

from __future__ import annotations

import importlib.util
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.schemas import EnrichRequest, EnrichResponse

pytestmark = pytest.mark.asyncio

ENTITY_REF = "res.partner:55"

# `app.engines.handlers` reaches the Perplexity SDK transitively (handlers ->
# enrichment_orchestrator -> perplexity_client -> `import perplexity`). The
# constitution gate runs `tests/contracts/` in an environment that installs only
# constellation-node-sdk — it prints "SKIP: No module named 'app'" for its own
# attestation step, so an unimportable app runtime is expected there, not a
# defect. Importing it at module scope would therefore break collection of every
# test in this file, including the ones that need no runtime at all.
#
# So: the payload/model contracts below run everywhere, and the dispatch tests
# import the handler lazily and declare the runtime they need. They are NOT
# marked `unit` (that marker means "no external dependencies"), so the gate
# deselects them and the full `pytest tests/` job — which installs the project —
# runs them. Removal trigger: the gate installing the project's runtime deps.
_HAS_PROVIDER_RUNTIME = importlib.util.find_spec("perplexity") is not None
requires_app_runtime = pytest.mark.skipif(
    not _HAS_PROVIDER_RUNTIME,
    reason="app.engines.handlers needs the Perplexity SDK; not installed in the contract gate env",
)


def _handle_converge():
    """Import the live dispatch entry point lazily. See the note above."""
    from app.engines.handlers import handle_converge

    return handle_converge


def _live_producer_payload(**overrides: Any) -> dict[str, Any]:
    """The exact wire payload the live Odoo builder emits.

    Identity rides ON the entity (`id` canonical, `_odoo_entity_id` the
    compatibility alias). There is no top-level `entity_id` and no
    `entity_snapshot`; `object_type` carries the domain; passes arrive as
    `max_variations`.
    """
    base: dict[str, Any] = {
        "entity": {
            "name": "Acme Recycling",
            "city": "Charlotte",
            "id": ENTITY_REF,
            "_odoo_entity_id": ENTITY_REF,
        },
        "object_type": "plasticos",
        "objective": "Full entity enrichment and inference",
        "max_variations": 5,
        "consensus_threshold": 0.80,
        "odoo": {
            "model": "plasticos.enrichment.run",
            "record_id": 7,
            "company_id": 1,
            "user_id": 2,
            "db_name": "plasticos",
            "correlation_id": "plasticos.enrichment.run:7",
        },
    }
    base.update(overrides)
    return base


def _converged() -> EnrichResponse:
    return EnrichResponse(
        state="completed",
        fields={"website": "https://acme.example", "annual_tonnage": 4200},
        confidence=0.88,
        pass_count=2,
        tokens_used=1200,
    )


@pytest.fixture
def converge_runtime():
    """Patch the collaborators around the handler, leaving dispatch itself real."""
    with (
        patch("app.engines.handlers.run_convergence_loop", new_callable=AsyncMock) as loop,
        patch("app.services.result_store.ResultStore.persist_enrich_response") as persist,
        patch("app.engines.packet_router.get_router") as router_factory,
        patch("app.services.event_emitter.get_emitter") as emitter_factory,
        patch("app.engines.handlers.get_settings", return_value=MagicMock()),
    ):
        loop.return_value = _converged()
        persist.return_value = None
        router = router_factory.return_value
        router.notify_graph_sync = AsyncMock(return_value=None)
        router.notify_score_invalidate = AsyncMock(return_value=None)
        emitter_factory.return_value.emit_enrichment_completed = AsyncMock(return_value=None)

        from app.services.side_effect_coordinator import get_side_effect_coordinator

        get_side_effect_coordinator().reset_for_tests()
        yield {"loop": loop, "persist": persist, "router": router}
        get_side_effect_coordinator().reset_for_tests()


@pytest.mark.unit
async def test_retired_compatibility_shape_is_not_a_valid_enrich_request() -> None:
    """One canonical contract: the pre-canonical dialect no longer validates."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EnrichRequest.model_validate(
            {"entity_id": ENTITY_REF, "domain": "plasticos", "entity_snapshot": {"name": "Acme"}}
        )


@pytest.mark.unit
async def test_live_payload_validates_as_a_canonical_enrich_request() -> None:
    """EIE1/EIE4 — the wire payload IS an EnrichRequest, identity included.

    Needs no app runtime, so it runs in the contract gate too: if the canonical
    branch could not accept this payload directly, the whole rail is wrong
    regardless of what dispatch does with it.
    """
    request = EnrichRequest.model_validate(_live_producer_payload())
    assert request.entity["id"] == ENTITY_REF
    assert request.entity["_odoo_entity_id"] == ENTITY_REF
    assert request.object_type == "plasticos"
    assert request.objective == "Full entity enrichment and inference"
    assert request.max_variations == 5
    assert request.consensus_threshold == 0.80


@requires_app_runtime
async def test_dispatch_routes_the_live_payload_to_the_canonical_branch(
    converge_runtime,
) -> None:
    await _handle_converge()("plasticos", _live_producer_payload())

    assert converge_runtime["loop"].await_count == 1


@requires_app_runtime
async def test_request_reaches_the_convergence_loop_untranslated(converge_runtime) -> None:
    """EIE4 — identity survives, and so does everything else the caller chose."""
    await _handle_converge()("plasticos", _live_producer_payload())

    request = converge_runtime["loop"].await_args.kwargs["request"]
    assert isinstance(request, EnrichRequest)
    assert request.entity["id"] == ENTITY_REF
    assert request.entity["_odoo_entity_id"] == ENTITY_REF
    assert request.entity["name"] == "Acme Recycling"
    assert request.object_type == "plasticos"
    assert request.objective == "Full entity enrichment and inference"
    assert request.max_variations == 5
    assert request.consensus_threshold == 0.80


@requires_app_runtime
async def test_response_is_exactly_an_enrich_response(converge_runtime) -> None:
    """EIE2 — no envelope, no field loss, no partner-allowlist truncation."""
    result = await _handle_converge()("plasticos", _live_producer_payload())

    assert result == _converged().model_dump()
    assert result["state"] == "completed"
    assert result["fields"]["annual_tonnage"] == 4200
    for obsolete in ("status", "final_fields", "writeback", "total_cost_usd"):
        assert obsolete not in result


@requires_app_runtime
async def test_canonical_converge_makes_zero_graph_calls(converge_runtime) -> None:
    """EIE14 — Graph is off the latency-bounded path; persistence is not."""
    await _handle_converge()("plasticos", _live_producer_payload())

    assert converge_runtime["router"].notify_graph_sync.await_count == 0
    assert converge_runtime["persist"].call_count == 1


@requires_app_runtime
async def test_retired_compatibility_dialect_fails_closed(converge_runtime) -> None:
    """EIE20 (retired) — the pre-canonical dialect is refused, never translated.

    The live Odoo producer emits only the canonical EnrichRequest. A payload in
    the retired `entity_snapshot` / top-level `entity_id` shape is answered as an
    invalid request: no convergence, no persistence, no Graph sync.
    """
    payload = {
        "entity_id": ENTITY_REF,
        "domain": "plasticos",
        "entity_snapshot": {"name": "Acme Recycling"},
    }

    result = await _handle_converge()("plasticos", payload)

    assert result["state"] == "failed"
    assert result["failure_reason"].startswith("invalid converge request")
    assert converge_runtime["loop"].await_count == 0
    assert converge_runtime["persist"].call_count == 0
    assert converge_runtime["router"].notify_graph_sync.await_count == 0


# Keys every derived-field provenance record carries. Additive to EnrichResponse:
# they live inside the free-form `inferences[*]` entries and `feature_vector`.
INFERENCE_PROVENANCE_KEYS = {
    "rule_id",
    "rule_version",
    "derivation_type",
    "inputs",
    "input_confidence",
    "rule_confidence",
    "confidence",
    "cascade_level",
}
INFERENCE_CONTEXT_KEYS = {
    "status",
    "engines",
    "rule_count",
    "derivation_edges",
    "rule_versions",
    "errors",
}


@requires_app_runtime
async def test_converge_inference_provenance_shape() -> None:
    """Domain inference rides the response additively, with complete provenance.

    The live loop runs with the repository's PlasticOS rules; only the provider
    and the side-effect sinks are replaced. The response must still be exactly an
    EnrichResponse, and every derived field must say which rule, at which
    rule-set version, from which inputs, at what confidence.
    """
    from pathlib import Path

    from app.engines.domain_yaml_reader import DomainYamlReader
    from app.services.side_effect_coordinator import get_side_effect_coordinator

    evidence = {
        "processed_polymers": ([{"code": "HDPE"}], 0.95),
        "contamination_tolerance": (0.02, 0.90),
        "capacity_tons_month": (500.0, 0.90),
    }

    async def provider(request, settings, kb_resolver=None, idem_store=None, **_kw):
        return EnrichResponse(
            state="completed",
            fields={k: v for k, (v, _c) in evidence.items()},
            confidence=0.9,
            tokens_used=100,
            feature_vector={"per_field_confidence": {k: c for k, (_v, c) in evidence.items()}},
        )

    domains = Path(__file__).resolve().parents[2] / "domains"
    with (
        patch("app.engines.handlers._domain_reader", DomainYamlReader(domains)),
        patch("app.engines.enrichment_orchestrator.enrich_entity", AsyncMock(side_effect=provider)),
        patch("app.services.result_store.ResultStore.persist_enrich_response"),
        patch("app.engines.packet_router.get_router") as router_factory,
        patch("app.services.event_emitter.get_emitter") as emitter_factory,
    ):
        router_factory.return_value.notify_graph_sync = AsyncMock()
        router_factory.return_value.notify_score_invalidate = AsyncMock()
        emitter_factory.return_value.emit_enrichment_completed = AsyncMock()
        get_side_effect_coordinator().reset_for_tests()
        result = await _handle_converge()("tenant-a", _live_producer_payload())
        get_side_effect_coordinator().reset_for_tests()

    assert EnrichResponse.model_validate(result).model_dump() == result
    assert set(result["feature_vector"]["inference"]) == INFERENCE_CONTEXT_KEYS
    provenance = result["inferences"][0]["inference_provenance"]
    assert set(provenance) == {"material_grade", "tier"}
    for record in provenance.values():
        assert set(record) == INFERENCE_PROVENANCE_KEYS
        assert record["rule_version"] == "plasticos-inference-0.4.0"
        assert record["confidence"] <= min(record["input_confidence"].values())
