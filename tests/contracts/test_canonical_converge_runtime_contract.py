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

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.engines.handlers import handle_converge
from app.models.schemas import EnrichRequest, EnrichResponse
from app.services.odoo_gate_converge import is_odoo_compat_converge_payload

pytestmark = pytest.mark.asyncio

ENTITY_REF = "res.partner:55"


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


async def test_live_payload_is_not_claimed_by_the_compatibility_adapter() -> None:
    """One canonical contract: the adapter cannot answer for a canonical payload."""
    assert is_odoo_compat_converge_payload(_live_producer_payload()) is False


async def test_dispatch_routes_the_live_payload_to_the_canonical_branch(
    converge_runtime,
) -> None:
    with patch(
        "app.engines.handlers._handle_odoo_compat_converge", new_callable=AsyncMock
    ) as compat:
        await handle_converge("plasticos", _live_producer_payload())

    assert compat.await_count == 0
    assert converge_runtime["loop"].await_count == 1


async def test_request_reaches_the_convergence_loop_untranslated(converge_runtime) -> None:
    """EIE4 — identity survives, and so does everything else the caller chose."""
    await handle_converge("plasticos", _live_producer_payload())

    request = converge_runtime["loop"].await_args.kwargs["request"]
    assert isinstance(request, EnrichRequest)
    assert request.entity["id"] == ENTITY_REF
    assert request.entity["_odoo_entity_id"] == ENTITY_REF
    assert request.entity["name"] == "Acme Recycling"
    assert request.object_type == "plasticos"
    assert request.objective == "Full entity enrichment and inference"
    assert request.max_variations == 5


async def test_response_is_exactly_an_enrich_response(converge_runtime) -> None:
    """EIE2 — no envelope, no field loss, no partner-allowlist truncation."""
    result = await handle_converge("plasticos", _live_producer_payload())

    assert result == _converged().model_dump()
    assert result["state"] == "completed"
    assert result["fields"]["annual_tonnage"] == 4200
    for obsolete in ("status", "final_fields", "writeback", "total_cost_usd"):
        assert obsolete not in result


async def test_canonical_converge_makes_zero_graph_calls(converge_runtime) -> None:
    """EIE14 — Graph is off the latency-bounded path; persistence is not."""
    await handle_converge("plasticos", _live_producer_payload())

    assert converge_runtime["router"].notify_graph_sync.await_count == 0
    assert converge_runtime["persist"].call_count == 1


async def test_compatibility_dialect_still_has_a_home(converge_runtime) -> None:
    """EIE20 — the compatibility branch is quarantined, not deleted."""
    payload = {
        "entity_id": ENTITY_REF,
        "domain": "plasticos",
        "entity_snapshot": {"name": "Acme Recycling"},
    }
    assert is_odoo_compat_converge_payload(payload) is True

    with patch(
        "app.engines.handlers._handle_odoo_compat_converge", new_callable=AsyncMock
    ) as compat:
        compat.return_value = {"state": "completed", "fields": {}}
        await handle_converge("plasticos", payload)

    assert compat.await_count == 1
