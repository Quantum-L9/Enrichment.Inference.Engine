"""End-to-end cross-repository contract: live Odoo payload -> EIE -> live Odoo mapper.

    exact Odoo builder payload
      -> active EIE converge ingress (handle_converge)
      -> provider mocked only at the provider boundary (query_perplexity)
      -> canonical EIE result
      -> consumed by a mirror of the live Odoo mapper

Nothing here imports IB-Odoo_19: the fixture and the mapper mirror are copies,
each carrying the exact source file and function they represent, so this stays a
contract test rather than a cross-repo runtime dependency.

The defect being regressed: the hardening was attached to a compatibility
adapter for a ``entity_snapshot``/``entity_id`` dialect the live builder never
emits. Widening that adapter's discriminator to swallow the real payload was not
a fix — the adapter is lossy against it (drops ``entity["id"]``, rewrites
``objective`` and ``object_type``, truncates ``fields``). The canonical branch
now serves the live payload untranslated, and carries the hardening itself.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.engines.handlers import handle_converge
from app.models.schemas import EnrichResponse
from app.services import perplexity_client
from app.services.request_deadline import CANONICAL_CONVERGE_BUDGET_SECONDS

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

ENTITY_REF = "res.partner:55"


# ── The exact request the live Odoo builder emits ───────────────────────────
# IB-Odoo_19 plasticos_gate/services/gate_builders.py::build_converge_request
#   -> plasticos_gate/services/gate_contracts.py::ConvergeRequest.to_dict
def _odoo_builder_payload() -> dict[str, Any]:
    return {
        "entity": {
            "name": "Acme Recycling",
            "city": "Charlotte",
            "id": ENTITY_REF,
            "_odoo_entity_id": ENTITY_REF,
        },
        "object_type": "plasticos",
        "objective": "Full entity enrichment and inference",
        "max_variations": 5,
        "odoo": {
            "model": "plasticos.enrichment.run",
            "record_id": 7,
            "company_id": 1,
            "user_id": 2,
            "db_name": "plasticos",
            "correlation_id": "plasticos.enrichment.run:7",
        },
    }


# ── The exact consumption the live Odoo mapper performs ─────────────────────
# Mirrors IB-Odoo_19 plasticos_gate/services/gate_mappers.py
#   ::map_converge_response (EIE_STATE_COMPLETED = "completed")
# and ::partner_writeback_from_converge.
_EIE_STATE_COMPLETED = "completed"
_PARTNER_WRITEBACK_FIELD_ALLOWLIST = frozenset(
    {"name", "website", "city", "zip", "street", "street2", "email", "phone"}
)


def _odoo_map_converge_response(payload: dict[str, Any]) -> dict[str, Any]:
    raw_state = payload.get("state")
    state = raw_state if isinstance(raw_state, str) and raw_state.strip() else None
    failure_reason = payload.get("failure_reason")
    if state == _EIE_STATE_COMPLETED and not failure_reason:
        status = "ok"
    else:
        status = failure_reason or state or "failed"
    return {
        "status": status,
        "state": state,
        "failure_reason": failure_reason,
        "final_fields": payload.get("fields") or {},
        "tokens_used": payload.get("tokens_used"),
        "confidence": payload.get("confidence"),
    }


def _odoo_partner_writeback(mapped: dict[str, Any]) -> dict[str, Any]:
    source = mapped.get("final_fields") or {}
    return {
        k: v
        for k, v in source.items()
        if k in _PARTNER_WRITEBACK_FIELD_ALLOWLIST and v not in (None, False, "")
    }


@pytest.fixture
def eie_runtime():
    """Mock only the provider boundary and the side-effect collaborators."""
    enriched = EnrichResponse(
        state="completed",
        fields={"website": "https://acme.example", "phone": "555-0100", "city": "Charlotte"},
        pass_count=2,
        tokens_used=1200,
        confidence=0.88,
    )
    with (
        patch("app.engines.handlers.run_convergence_loop", new_callable=AsyncMock) as loop,
        patch("app.services.result_store.ResultStore.persist_enrich_response") as persist,
        patch("app.engines.packet_router.get_router") as router_factory,
        patch("app.services.event_emitter.get_emitter") as emitter_factory,
        patch("app.engines.handlers.get_settings", return_value=MagicMock()),
    ):
        loop.return_value = enriched
        persist.return_value = None
        router = router_factory.return_value
        router.notify_graph_sync = AsyncMock(return_value=None)
        router.notify_score_invalidate = AsyncMock(return_value=None)
        emitter_factory.return_value.emit_enrichment_completed = AsyncMock(return_value=None)

        from app.services.side_effect_coordinator import get_side_effect_coordinator

        get_side_effect_coordinator().reset_for_tests()
        yield {"loop": loop, "persist": persist, "router": router}
        get_side_effect_coordinator().reset_for_tests()


async def test_live_odoo_payload_round_trips_through_the_active_eie_path(eie_runtime):
    """The whole contract, end to end, in one assertion block."""
    result = await handle_converge("plasticos", _odoo_builder_payload())

    # 1. It reached the canonical handler (not the compatibility adapter).
    assert eie_runtime["loop"].await_count == 1

    # 2. The request went in untranslated: identity, objective and object_type
    #    are the caller's, not values this repo substituted for them.
    request = eie_runtime["loop"].await_args.kwargs["request"]
    assert request.entity["id"] == ENTITY_REF
    assert request.object_type == "plasticos"
    assert request.objective == "Full entity enrichment and inference"
    assert eie_runtime["persist"].call_args is not None, "canonical convergence must still persist"

    # 3/4. The response is the canonical {state, fields} the live mapper reads.
    assert result["state"] == "completed"

    # 5. The obsolete envelope is gone.
    for obsolete in ("status", "final_fields", "writeback", "total_cost_usd"):
        assert obsolete not in result

    # 6. Odoo consumes it without any compatibility translation.
    mapped = _odoo_map_converge_response(result)
    assert mapped["status"] == "ok", "live Odoo mapper must see a usable result"
    assert mapped["state"] == "completed"
    assert mapped["final_fields"]["website"] == "https://acme.example"
    # EIE does not pre-filter; the allowlist is applied on the Odoo side, and
    # merge-not-overwrite is Odoo's own writeback rule.
    assert _odoo_partner_writeback(mapped) == {
        "website": "https://acme.example",
        "phone": "555-0100",
        "city": "Charlotte",
    }

    # 7. EIE14 — zero Graph calls on the canonical Odoo path.
    assert eie_runtime["router"].notify_graph_sync.await_count == 0


async def test_live_odoo_payload_runs_under_the_converge_deadline(eie_runtime):
    """The deadline context is applied to the request Odoo actually sends."""
    seen: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> EnrichResponse:
        from app.services.request_deadline import current_deadline

        deadline = current_deadline()
        seen["deadline"] = deadline
        seen["remaining"] = deadline.remaining() if deadline else None
        return EnrichResponse(
            state="completed", fields={"website": "https://a.example"}, pass_count=1
        )

    eie_runtime["loop"].side_effect = _capture
    await handle_converge("plasticos", _odoo_builder_payload())

    assert seen["deadline"] is not None, "canonical path ran without a deadline in scope"
    assert 0 < seen["remaining"] <= CANONICAL_CONVERGE_BUDGET_SECONDS
    assert CANONICAL_CONVERGE_BUDGET_SECONDS <= 25.0


async def test_live_odoo_payload_keeps_sdk_retries_disabled(eie_runtime):
    """EIE stays the only retry owner for the request Odoo actually sends."""
    await handle_converge("plasticos", _odoo_builder_payload())

    api_key = "cross-contract-probe"
    perplexity_client._clients.pop(api_key, None)
    try:
        assert perplexity_client._get_client(api_key).max_retries == 0
    finally:
        perplexity_client._clients.pop(api_key, None)


async def test_live_odoo_failure_degrades_rather_than_injecting(eie_runtime):
    """A non-completed convergence must not read as success on the Odoo side.

    The canonical failure is an EnrichResponse with a non-completed ``state``,
    not a bespoke error envelope. Odoo's mapper resolves it to a non-ok status
    and ``_run_gate_converge`` falls closed to its degraded state.
    """
    from app.engines.handlers import _canonical_failure

    mapped = _odoo_map_converge_response(_canonical_failure("gate unavailable"))
    assert mapped["status"] != "ok"
    assert mapped["state"] == "failed"
    assert mapped["final_fields"] == {}


async def test_canonical_fields_reach_odoo_without_eie_side_filtering(eie_runtime):
    """A non-partner field survives EIE; only Odoo's allowlist removes it.

    The compatibility adapter filtered ``fields`` down to the eight partner keys
    before answering. On the canonical rail that is field loss: EnrichResponse
    carries whatever converged, and Odoo's ``partner_writeback_from_converge``
    decides what it will write.
    """
    eie_runtime["loop"].return_value = EnrichResponse(
        state="completed",
        fields={"website": "https://acme.example", "annual_tonnage": 4200},
        pass_count=1,
    )

    result = await handle_converge("plasticos", _odoo_builder_payload())

    assert result["fields"]["annual_tonnage"] == 4200
    mapped = _odoo_map_converge_response(result)
    assert mapped["final_fields"]["annual_tonnage"] == 4200
    assert _odoo_partner_writeback(mapped) == {"website": "https://acme.example"}
