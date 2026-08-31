"""TASK-021: one persistence/dispatch/event per semantic request."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.side_effect_coordinator import (
    SideEffectCoordinator,
    semantic_side_effect_key,
)


@pytest.mark.asyncio
async def test_commit_once_per_key() -> None:
    coord = SideEffectCoordinator()
    settings = SimpleNamespace(gate_url="https://gate.example")
    response = {
        "state": "completed",
        "fields": {"polymer_type": "HDPE"},
        "confidence": 0.9,
        "tokens_used": 3,
    }

    persist = AsyncMock()
    router = MagicMock()
    router.notify_graph_sync = AsyncMock(return_value={"status": "ok"})
    router.notify_score_invalidate = AsyncMock()
    emitter = MagicMock()
    emitter.emit_enrichment_completed = AsyncMock()

    with (
        patch("app.models.schemas.EnrichResponse.model_validate", return_value=MagicMock()),
        patch("app.services.result_store.ResultStore") as result_store_cls,
        patch("app.engines.packet_router.get_router", return_value=router),
        patch("app.services.event_emitter.get_emitter", return_value=emitter),
    ):
        result_store_cls.return_value.persist_enrich_response = persist
        first = await coord.commit_after_enrich(
            tenant="t1",
            entity_id="e1",
            object_type="Contact",
            domain="plasticos",
            response_dict=response,
            settings=settings,
            idempotency_key="idem-1",
        )
        second = await coord.commit_after_enrich(
            tenant="t1",
            entity_id="e1",
            object_type="Contact",
            domain="plasticos",
            response_dict=response,
            settings=settings,
            idempotency_key="idem-1",
        )

    assert first.skipped is False
    assert second.skipped is True
    assert persist.await_count == 1
    assert router.notify_graph_sync.await_count == 1
    assert router.notify_score_invalidate.await_count == 1
    assert emitter.emit_enrichment_completed.await_count == 1


def test_semantic_key_prefers_logical_identity_over_packet_identity() -> None:
    """ADR-EIE-008 — packet id identifies an attempt, not a logical operation.

    A transport replay of the SAME logical operation carries a NEW packet id.
    Keying on it therefore cannot recognise the replay it exists to suppress,
    while it CAN split one operation into two. The caller's idempotency key is
    the only identity that survives a retry, so it wins whenever both present.
    """
    assert (
        semantic_side_effect_key(tenant="t", entity_id="e", packet_id="p-1", idempotency_key="i-1")
        == "idem:t:i-1:enrich"
    )


def test_semantic_key_falls_back_to_packet_identity_only() -> None:
    """Without a logical key, one packet's effects still commit only once."""
    assert (
        semantic_side_effect_key(tenant="t", entity_id="e", packet_id="p-1") == "pkt:t:p-1:enrich"
    )


def test_semantic_key_is_tenant_scoped() -> None:
    """The same raw caller key under two tenants is two operations."""
    a = semantic_side_effect_key(tenant="tenant-a", entity_id="e", idempotency_key="shared")
    b = semantic_side_effect_key(tenant="tenant-b", entity_id="e", idempotency_key="shared")
    assert a != b


def test_semantic_key_never_collapses_to_entity_identity() -> None:
    """ADR-EIE-008 — no entity-only fallback.

    The retired fallback hashed tenant|entity|action, which made the FIRST
    enrichment of an entity permanently mark every later one a duplicate. None
    means "no completion dedupe", which is honest; a fabricated entity key is
    silent data loss.
    """
    assert semantic_side_effect_key(tenant="t", entity_id="res.partner:55") is None
    assert (
        semantic_side_effect_key(tenant="t", entity_id="res.partner:55", idempotency_key="  ")
        is None
    )
