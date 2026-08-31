"""Contract-bound surface for TASK-021 side-effect coordinator."""

from __future__ import annotations

from app.services.side_effect_coordinator import (
    SideEffectCoordinator,
    get_side_effect_coordinator,
    semantic_side_effect_key,
)


def test_coordinator_singleton_stable() -> None:
    a = get_side_effect_coordinator()
    b = get_side_effect_coordinator()
    assert a is b
    assert isinstance(a, SideEffectCoordinator)


def test_semantic_key_contract() -> None:
    """Logical operation identity first, packet identity second, entity never."""
    assert (
        semantic_side_effect_key(tenant="t", entity_id="e", idempotency_key="abc")
        == "idem:t:abc:enrich"
    )
    assert (
        semantic_side_effect_key(tenant="t", entity_id="e", packet_id="abc") == "pkt:t:abc:enrich"
    )
    assert semantic_side_effect_key(tenant="t", entity_id="e") is None
