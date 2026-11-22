"""A5: a budgeted sweep through the real converge path, with CEG readiness and graph feedback.

Campaign EIE-CEG-GATE-COLLAB slice A5 (docs/contracts/enrichment-sweep.md). The
sweep is driven through `handle_enrichment_sweep` into the canonical
`handle_converge` and the real `run_convergence_loop`. Only CEG's
`health-assess` answer, the provider call and persistence are doubles. Proved
here, at unit level:

* readiness comes only from CEG; with no answer nothing is enriched, nothing is
  reserved, and no local estimate stands in (P31);
* only fields the tenant's cartridge declares become converge targets, and a
  graph-return target for an undeclared field is refused, not seeded (A5.3);
* each enriched candidate costs one converge and one reservation, and graph
  feedback adds no provider call to it; feedback never starts a sweep;
* a replay, including one after a restart, spends nothing (P32);
* nothing writes back to the consumer: no CRM client or writeback orchestrator
  is constructed on the sweep path (A5.5).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from constellation_node_sdk.transport import create_transport_packet

from app.core.config import get_settings
from app.engines import handlers as eie_handlers
from app.engines import sweep_handlers
from app.models.schemas import EnrichRequest, EnrichResponse
from app.services.graph_return_channel import GraphReturnChannel, handle_graph_inference_result
from app.services.side_effect_coordinator import SideEffectReport
from tests.unit.test_enrichment_sweep import (
    _ENV,
    FIELDS,
    NODE,
    NOW,
    READER,
    TENANT,
    Graph,
    MemoryStore,
    candidates,
    fetch_all,
    healthy,
)

DECLARED = FIELDS[0]
UNDECLARED = "not_a_declared_field"


@pytest.fixture(autouse=True)
def _env() -> Iterator[None]:
    with patch.dict(os.environ, _ENV):
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


class _Provider:
    """Stands in for the provider call only; counts every call."""

    def __init__(self) -> None:
        self.requests: list[EnrichRequest] = []

    async def __call__(self, request: EnrichRequest, *args: Any, **kwargs: Any) -> EnrichResponse:
        self.requests.append(request)
        return EnrichResponse(fields={}, confidence=0.5, tokens_used=10, state="completed")


class _NoWritebackError(Exception):
    pass


def _refuse(*args: Any, **kwargs: Any) -> None:
    raise _NoWritebackError("the sweep path reached a consumer writeback")


@pytest.fixture
def live() -> Iterator[tuple[_Provider, MemoryStore, list[dict[str, Any]]]]:
    """The real converge handler and loop; provider, persistence and CEG are doubles."""
    saved = (eie_handlers._kb, eie_handlers._idem, eie_handlers._domain_reader)
    provider, store, persisted = _Provider(), MemoryStore(), []

    async def _persist(*args: Any, **kwargs: Any) -> SideEffectReport:
        persisted.append(kwargs)
        return SideEffectReport(key="no-persist")

    GraphReturnChannel.reset_instance()
    eie_handlers.init_handlers(kb=None, idem=None, domain_reader=READER)
    try:
        with (
            patch("app.engines.enrichment_orchestrator.enrich_entity", provider),
            patch("app.engines.handlers._persist_and_sync", _persist),
            patch("app.services.crm.writeback.WriteBackOrchestrator.__init__", _refuse),
            patch("app.services.crm.odoo_client.OdooClient.__init__", _refuse),
            patch("app.services.crm.salesforce_client.SalesforceClient.__init__", _refuse),
        ):
            yield provider, store, persisted
    finally:
        GraphReturnChannel.reset_instance()
        eie_handlers._kb, eie_handlers._idem, eie_handlers._domain_reader = saved


def _wire(store: MemoryStore, graph: Graph) -> None:
    # converge is left unset: the sweep runs the canonical converge handler.
    sweep_handlers.init_sweep(reader=READER, store=store, assess=graph, clock=lambda: NOW)


async def _sweep(n: int, key: str, budget: float = 10.0) -> dict[str, Any]:
    reply = await sweep_handlers.handle_enrichment_sweep(
        TENANT, {"candidates": candidates(n), "daily_budget_usd": budget, "idempotency_key": key}
    )
    await sweep_handlers.wait_for_background_sweeps()
    return reply


def _gaps(*names: str) -> Graph:
    return Graph(
        lambda payload: {
            "entities": [
                healthy(e["entity_id"], gaps=list(names), targets=[], score=0.3)
                for e in payload["entities"]
            ]
        }
    )


@pytest.mark.asyncio
async def test_ceg_gaps_drive_real_converges_with_only_declared_targets(live: Any) -> None:
    provider, store, persisted = live
    _wire(store, _gaps(DECLARED, UNDECLARED))
    reply = await _sweep(2, "s-1")
    assert len(reply["accepted"]) == 2
    results = await fetch_all(reply["sweep_id"])
    assert [r["entity_id"] for r in results["results"]] == ["rec-0000", "rec-0001"]
    assert results["stats"]["enriched"] == 2
    assert results["stats"]["targets_dropped_undeclared"] == 2
    # One converge -- one persisted result -- per enriched candidate, each
    # bounded by the loop's own pass limit.
    assert len(persisted) == 2
    assert len(provider.requests) >= 2
    # The sweep's spend is the ledger's, within its budget.
    assert results["budget"]["spent_usd"] <= results["budget"]["effective_usd"]


@pytest.mark.asyncio
async def test_no_ceg_readiness_means_no_converge_no_reservation_no_local_guess(
    live: Any,
) -> None:
    provider, store, _ = live
    _wire(store, Graph(raises=ConnectionError("CEG unreachable")))
    reply = await _sweep(3, "s-2")
    results = await fetch_all(reply["sweep_id"])
    assert results["results"] == []
    assert {s["reason"] for s in results["skipped"]} == {"readiness_failed"}
    assert provider.requests == []
    assert store.ledgers == {}  # nothing reserved, nothing spent


@pytest.mark.asyncio
async def test_a_disabled_ceg_answer_is_not_replaced_by_a_local_estimate(live: Any) -> None:
    provider, store, _ = live
    _wire(store, Graph({"status": "disabled"}))
    results = await fetch_all((await _sweep(2, "s-3"))["sweep_id"])
    assert {s["reason"] for s in results["skipped"]} == {"readiness_disabled"}
    assert provider.requests == []
    assert store.ledgers == {}


def _feedback(entity_id: str, field: str, value: Any) -> Any:
    return create_transport_packet(
        action="graph-inference-result",
        payload={
            "inference_outputs": [
                {
                    "entity_id": entity_id,
                    "field": field,
                    "value": value,
                    "confidence": 0.9,
                    "rule": "r1",
                }
            ]
        },
        tenant=TENANT,
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )


async def _calls_for_one_converge(live: Any, key: str, *, feedback: bool) -> int:
    provider, store, _ = live
    before = len(provider.requests)
    if feedback:
        for field, value in ((DECLARED, "from-graph"), (UNDECLARED, "never")):
            packet = _feedback("rec-0000", field, value)
            await handle_graph_inference_result(TENANT, packet.payload, packet)
        # Intake neither starts a sweep nor calls the provider.
        assert len(provider.requests) == before
        assert key not in {r.idempotency_key for r in store.sweeps.values()}
    _wire(store, _gaps(DECLARED))
    await _sweep(1, key)
    return len(provider.requests) - before


@pytest.mark.asyncio
async def test_graph_feedback_is_consumed_by_the_sweeps_converge_and_orders_no_call(
    live: Any,
) -> None:
    provider, _, _ = live
    without = await _calls_for_one_converge(live, "s-4a", feedback=False)
    first_seen = len(provider.requests)
    with_feedback = await _calls_for_one_converge(live, "s-4b", feedback=True)
    # Feedback seeds the pass and may let it converge sooner; it never adds a call.
    assert 1 <= with_feedback <= without
    seen = provider.requests[first_seen].entity
    assert seen.get(DECLARED) == "from-graph"  # the declared target seeded the pass
    assert UNDECLARED not in seen  # the undeclared one never did
    assert GraphReturnChannel.get_instance().take_for_entity(TENANT, "rec-0000") == []


@pytest.mark.asyncio
async def test_a_replay_after_a_restart_spends_nothing_and_runs_nothing(live: Any) -> None:
    provider, store, _ = live
    _wire(store, _gaps(DECLARED))
    first = await _sweep(2, "s-5")
    spent = store.ledgers[(TENANT, NOW.date())]
    calls = len(provider.requests)
    # A new process: fresh wiring and channel, the same durable store.
    GraphReturnChannel.reset_instance()
    _wire(store, _gaps(DECLARED))
    again = await _sweep(2, "s-5", budget=50.0)
    assert again == first
    assert len(provider.requests) == calls
    assert store.ledgers[(TENANT, NOW.date())] == spent


@pytest.mark.asyncio
async def test_a_reservation_the_budget_cannot_cover_is_never_spent(live: Any) -> None:
    provider, store, _ = live
    _wire(store, _gaps(DECLARED))
    # One converge reserves $1.00 (fixture settings); the budget is $0.50.
    results = await fetch_all((await _sweep(3, "s-6", budget=0.5))["sweep_id"])
    assert {s["reason"] for s in results["skipped"]} == {"budget_exhausted"}
    assert results["results"] == []
    assert provider.requests == []
    assert (TENANT, NOW.date()) not in store.ledgers or store.ledgers[
        (TENANT, NOW.date())
    ].spent_usd == Decimal(0)


@pytest.mark.asyncio
async def test_spend_is_the_actual_cost_and_every_reservation_is_released(live: Any) -> None:
    _, store, _ = live
    _wire(store, _gaps(DECLARED))
    await _sweep(2, "s-7", budget=3.0)
    ledger = store.ledgers[(TENANT, NOW.date())]
    assert ledger.reserved_usd == Decimal(0)
    assert Decimal(0) < ledger.spent_usd <= Decimal(3)


def test_the_sweep_stays_off_by_default() -> None:
    with patch.dict(os.environ, {"L9_ENRICHMENT_SWEEP_ENABLED": ""}, clear=False):
        os.environ.pop("L9_ENRICHMENT_SWEEP_ENABLED", None)
        get_settings.cache_clear()
        assert get_settings().l9_enrichment_sweep_enabled is False


def test_the_node_under_test_comes_from_the_cartridge() -> None:
    assert NODE in READER.load(TENANT).node_schemas
