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


def test_semantic_key_prefers_packet_id() -> None:
    assert semantic_side_effect_key(
        tenant="t", entity_id="e", packet_id="p-1", idempotency_key="i-1"
    ).startswith("pkt:p-1")
