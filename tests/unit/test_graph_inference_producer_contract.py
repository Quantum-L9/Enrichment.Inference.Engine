"""EIE-008 — the consumer side must accept exactly what CEG now produces.

EIE advertises `graph-inference-result` to Gate and implements the whole
consumer side: packet validation, per-tenant queues, target extraction, a 0.55
confidence floor, injection into the convergence loop. Nothing in
Cognitive.Engine.Graphs ever constructed such a packet, so the loop had no
producer — the E2E drove the action from a synthetic client instead.

CEG's `engine/gate_egress.emit_graph_inference_result` is that producer now.
This module pins the wire shape it sends, from EIE's side of the seam: the two
repositories cannot import each other, so a change on either side that the
other cannot read has to be caught here. The elements below are what
`build_inference_outputs` emits, including the two keys CEG adds beyond EIE's
minimum (`provenance`, `rationale`), which must be ignored rather than rejected.

EIE-POST-F001/F004: the queue test below only proves submit/drain mechanics.
The loop closes only when the converge path *consumes* the queue, and nothing
did. The reachability tests at the end enter through the SDK-registered
`graph-inference-result` and `converge` handlers and run the real
`run_convergence_loop`. Only the provider call and persistence are stubbed. A
queued target must reach the pass request and the response, and it must stay
within its tenant and entity.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from constellation_node_sdk.runtime import handlers as sdk_handlers
from constellation_node_sdk.runtime.handlers import get_handler
from constellation_node_sdk.transport import create_transport_packet
from constellation_node_sdk.transport.errors import TransportValidationError

from app.engines import handlers as eie_handlers
from app.engines import orchestration_layer
from app.models.schemas import EnrichRequest, EnrichResponse
from app.services.chassis_handlers import register_all_handlers
from app.services.graph_return_channel import (
    CONFIDENCE_FLOOR,
    GraphReturnChannel,
    extract_targets_from_packet,
    handle_graph_inference_result,
    validate_graph_inference_packet,
)

TENANT = "acme"


def _ceg_output(
    field: str, value: object, confidence: float, rule: str, entity_id: str = "e-1"
) -> dict:
    """One element exactly as CEG's build_inference_outputs emits it."""
    return {
        "entity_id": entity_id,
        "field": field,
        "value": value,
        "confidence": confidence,
        "rule": rule,
        "provenance": "inference",
        "rationale": "employee_count=120",
    }


def _packet(outputs: list[dict], tenant: str = TENANT):
    return create_transport_packet(
        action="graph-inference-result",
        payload={"inference_outputs": outputs},
        tenant=tenant,
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )


def test_a_ceg_shaped_packet_validates() -> None:
    validate_graph_inference_packet(_packet([_ceg_output("tier", "mid_market", 0.85, "size")]))


def test_ceg_extra_keys_are_ignored_not_rejected() -> None:
    """CEG sends provenance and rationale beyond EIE's minimum."""
    targets = extract_targets_from_packet(
        _packet([_ceg_output("tier", "mid_market", 0.85, "size")])
    )
    assert len(targets) == 1
    target = targets[0]
    assert target.entity_id == "e-1"
    assert target.field_name == "tier"
    assert target.seed_value == "mid_market"
    assert target.source_confidence == 0.85
    assert target.origin_inference_rule == "size"
    assert target.tenant_id == TENANT


def test_both_sides_agree_on_the_confidence_floor() -> None:
    """CEG filters at INFERENCE_CONFIDENCE_FLOOR before sending; the numbers
    must match or one side silently discards what the other paid to send."""
    assert CONFIDENCE_FLOOR == 0.55


def test_an_output_below_the_floor_is_dropped_here_too() -> None:
    targets = extract_targets_from_packet(
        _packet(
            [
                _ceg_output("kept", "a", 0.56, "r1"),
                _ceg_output("dropped", "b", 0.54, "r2"),
            ]
        )
    )
    assert [t.field_name for t in targets] == ["kept"]


def test_an_empty_output_list_is_valid_and_queues_nothing() -> None:
    """Which is why CEG skips the Gate round trip rather than sending one."""
    assert extract_targets_from_packet(_packet([])) == []


def test_a_packet_for_another_action_is_refused() -> None:
    packet = create_transport_packet(
        action="converge",
        payload={"inference_outputs": []},
        tenant=TENANT,
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )
    with pytest.raises(TransportValidationError, match="graph-inference-result"):
        validate_graph_inference_packet(packet)


@pytest.mark.asyncio
async def test_submitted_targets_reach_the_tenant_queue() -> None:
    """Queue mechanics only. Consumption is proven by the reachability tests."""
    GraphReturnChannel.reset_instance()
    channel = GraphReturnChannel.get_instance()
    try:
        queued = await channel.submit(_packet([_ceg_output("tier", "small", 0.9, "size")]))
        assert queued == 1
        drained = await channel.drain(tenant_id=TENANT, timeout=0.5)
        assert [t.field_name for t in drained] == ["tier"]
    finally:
        GraphReturnChannel.reset_instance()


# ── EIE-POST-F001/F004: handler -> channel -> live convergence ─────────────

ENTITY = "res.partner:55"


def _converge_payload(entity_id: str = ENTITY) -> dict[str, Any]:
    """The canonical Odoo converge payload: identity rides on the entity."""
    return {
        "entity": {"name": "Acme Recycling", "id": entity_id, "_odoo_entity_id": entity_id},
        "object_type": "plasticos",
        "objective": "Full entity enrichment and inference",
        "max_passes": 1,
    }


class _RecordingEnricher:
    """Stands in for the provider call only; records every pass request."""

    def __init__(self) -> None:
        self.requests: list[EnrichRequest] = []

    async def __call__(
        self, request: EnrichRequest, settings, kb_resolver, idem_store, sonar_config=None
    ) -> EnrichResponse:
        self.requests.append(request)
        return EnrichResponse(
            fields={"website": "acme.example"}, confidence=0.9, tokens_used=10, state="completed"
        )


@pytest.fixture
def live_runtime() -> Iterator[_RecordingEnricher]:
    """Production handler registration, the real convergence loop, a fresh channel."""
    registry = dict(sdk_handlers._HANDLER_REGISTRY)
    saved = (eie_handlers._kb, eie_handlers._idem, eie_handlers._domain_reader)
    enricher = _RecordingEnricher()

    async def _no_persist(*args: Any, **kwargs: Any) -> None:
        return None

    GraphReturnChannel.reset_instance()
    try:
        register_all_handlers()
        orchestration_layer.register(kb=None)
        with (
            patch("app.engines.enrichment_orchestrator.enrich_entity", enricher),
            patch("app.engines.handlers._persist_and_sync", _no_persist),
        ):
            yield enricher
    finally:
        GraphReturnChannel.reset_instance()
        eie_handlers._kb, eie_handlers._idem, eie_handlers._domain_reader = saved
        sdk_handlers._HANDLER_REGISTRY.clear()
        sdk_handlers._HANDLER_REGISTRY.update(registry)


async def _deliver(outputs: list[dict], tenant: str = TENANT) -> dict[str, Any]:
    """Deliver a packet the way the SDK runtime does: via the registered handler."""
    handler = get_handler("graph-inference-result")
    assert handler is handle_graph_inference_result
    packet = _packet(outputs, tenant=tenant)
    result: dict[str, Any] = await handler(packet.tenant.org_id, packet.payload, packet)
    return result


async def _converge(tenant: str = TENANT, entity_id: str = ENTITY) -> dict[str, Any]:
    handler = get_handler("converge")
    assert handler is eie_handlers.handle_converge
    result: dict[str, Any] = await handler(tenant, _converge_payload(entity_id))
    return result


@pytest.mark.asyncio
async def test_graph_result_reaches_the_live_convergence_pass(
    live_runtime: _RecordingEnricher,
) -> None:
    """A queued target is consumed by the converge path, not by a test drain."""
    ack = await _deliver([_ceg_output("tier", "mid_market", 0.85, "size", entity_id=ENTITY)])
    assert ack["targets_queued"] == 1

    result = await _converge()

    assert result["state"] == "completed", result.get("failure_reason")
    # It shaped the pass: the provider saw the graph value in the entity.
    (pass_request,) = live_runtime.requests
    assert pass_request.entity["tier"] == "mid_market"
    # And it is in the converged result, attributed to the graph.
    assert result["fields"]["tier"] == "mid_market"
    assert result["inferences"][0]["graph_seeded"] == ["tier"]
    assert GraphReturnChannel.get_instance().stats()["queue_sizes"][TENANT] == 0


@pytest.mark.asyncio
async def test_graph_result_stays_inside_its_tenant_and_entity(
    live_runtime: _RecordingEnricher,
) -> None:
    await _deliver([_ceg_output("tier", "other_entity", 0.9, "size", entity_id="res.partner:99")])
    await _deliver(
        [_ceg_output("tier", "other_tenant", 0.9, "size", entity_id=ENTITY)], tenant="globex"
    )

    result = await _converge()

    assert "tier" not in result["fields"]
    assert "tier" not in live_runtime.requests[0].entity
    # Neither foreign target was consumed; both wait for their own converge.
    sizes = GraphReturnChannel.get_instance().stats()["queue_sizes"]
    assert sizes == {TENANT: 1, "globex": 1}

    other = await _converge(entity_id="res.partner:99")
    assert other["fields"]["tier"] == "other_entity"


@pytest.mark.asyncio
async def test_graph_result_never_overrides_a_more_confident_value(
    live_runtime: _RecordingEnricher,
) -> None:
    """Graph inference seeds convergence; it does not demote better evidence."""
    await _deliver(
        [
            _ceg_output("tier", "mid_market", 0.60, "size", entity_id=ENTITY),
            _ceg_output("tier", "enterprise", 0.80, "size_v2", entity_id=ENTITY),
        ]
    )

    result = await _converge()

    assert result["fields"]["tier"] == "enterprise"
