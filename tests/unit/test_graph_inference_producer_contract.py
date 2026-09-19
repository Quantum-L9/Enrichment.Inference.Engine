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
"""

from __future__ import annotations

import pytest
from constellation_node_sdk.transport import create_transport_packet
from constellation_node_sdk.transport.errors import TransportValidationError

from app.services.graph_return_channel import (
    CONFIDENCE_FLOOR,
    GraphReturnChannel,
    extract_targets_from_packet,
    validate_graph_inference_packet,
)

TENANT = "acme"


def _ceg_output(field: str, value: object, confidence: float, rule: str) -> dict:
    """One element exactly as CEG's build_inference_outputs emits it."""
    return {
        "entity_id": "e-1",
        "field": field,
        "value": value,
        "confidence": confidence,
        "rule": rule,
        "provenance": "inference",
        "rationale": "employee_count=120",
    }


def _packet(outputs: list[dict]):
    return create_transport_packet(
        action="graph-inference-result",
        payload={"inference_outputs": outputs},
        tenant=TENANT,
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
    """End of the loop: a CEG-shaped packet lands in the convergence channel."""
    GraphReturnChannel.reset_instance()
    channel = GraphReturnChannel.get_instance()
    try:
        queued = await channel.submit(_packet([_ceg_output("tier", "small", 0.9, "size")]))
        assert queued == 1
        drained = await channel.drain(tenant_id=TENANT, timeout=0.5)
        assert [t.field_name for t in drained] == ["tier"]
    finally:
        GraphReturnChannel.reset_instance()
