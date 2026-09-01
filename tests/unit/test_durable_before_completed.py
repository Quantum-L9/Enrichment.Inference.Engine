"""`completed` means durable, and tenant isolation holds on the canonical rail.

Both invariants were asserted before this suite existed and neither held on the
real Gate -> EIE rail:

1. A converge whose persistence failed still answered `state="completed"` with
   no failure_reason, because `_persist_and_sync` was fire-and-forward and the
   coordinator swallowed the persist exception. Gate turned that into a success
   and the enrichment was lost silently.
2. The once-per-key guard recorded the key anyway, so the retry that would have
   recovered the lost work was suppressed as a duplicate.
"""

from __future__ import annotations

import os

os.environ.update(
    {
        "PERPLEXITY_API_KEY": "test-key",
        "API_SECRET_KEY": "test-secret-key-32-chars-long!!",
        "KB_DIR": "./kb",
    }
)

from unittest.mock import AsyncMock, patch

import pytest

from app.services.side_effect_coordinator import (
    PersistenceRequiredError,
    SideEffectCoordinator,
    semantic_side_effect_key,
)


class _BoomStore:
    def __init__(self) -> None:
        self.calls = 0

    async def persist_enrich_response(self, **_kwargs):
        self.calls += 1
        raise RuntimeError("database unavailable")


def _patch_store(store):
    return patch("app.services.result_store.ResultStore", return_value=store)


def _response() -> dict:
    return {"fields": {"industry": "plastics"}, "confidence": 0.9, "state": "completed"}


async def _commit(coordinator, store, **kwargs):
    with (
        _patch_store(store),
        patch("app.engines.packet_router.get_router") as router,
        patch("app.services.event_emitter.get_emitter") as emitter,
    ):
        router.return_value.notify_graph_sync = AsyncMock()
        router.return_value.notify_score_invalidate = AsyncMock()
        emitter.return_value.emit_enrichment_completed = AsyncMock()
        return await coordinator.commit_after_enrich(
            tenant=kwargs.pop("tenant", "acme"),
            entity_id="res.partner:1",
            object_type="res.partner",
            domain="plastics",
            response_dict=_response(),
            settings=object(),
            **kwargs,
        )


# ── 1. Durability is a precondition of the answer ──────────


async def test_required_persistence_failure_raises_rather_than_reporting_success():
    coordinator = SideEffectCoordinator()
    with pytest.raises(PersistenceRequiredError, match="database unavailable"):
        await _commit(coordinator, _BoomStore(), idempotency_key="k1", require_persistence=True)


async def test_persistence_failure_stays_fire_and_forward_by_default():
    """Non-canonical callers keep the old behaviour: logged, never raised."""
    coordinator = SideEffectCoordinator()
    report = await _commit(coordinator, _BoomStore(), idempotency_key="k1")
    assert report.persisted is False
    assert any(e.startswith("persist:") for e in report.errors)


# ── 2. A failed operation leaves no completion marker ──────


async def test_failed_required_persistence_leaves_the_key_retryable():
    """The retry must not be suppressed as a duplicate of the failed attempt."""
    coordinator = SideEffectCoordinator()
    boom = _BoomStore()

    with pytest.raises(PersistenceRequiredError):
        await _commit(coordinator, boom, idempotency_key="k2", require_persistence=True)

    key = semantic_side_effect_key(
        tenant="acme", entity_id="res.partner:1", action="enrich", idempotency_key="k2"
    )
    assert key not in coordinator._completed, "a non-durable result must leave no marker"

    ok = AsyncMock()
    ok.persist_enrich_response = AsyncMock()
    report = await _commit(coordinator, ok, idempotency_key="k2", require_persistence=True)
    assert report.persisted is True
    assert report.skipped is False, "the retry was suppressed as a duplicate"
    assert boom.calls == 1


async def test_successful_required_persistence_still_dedups():
    coordinator = SideEffectCoordinator()
    ok = AsyncMock()
    ok.persist_enrich_response = AsyncMock()

    first = await _commit(coordinator, ok, idempotency_key="k3", require_persistence=True)
    second = await _commit(coordinator, ok, idempotency_key="k3", require_persistence=True)
    assert first.persisted is True
    assert second.skipped is True


# ── 3. No downstream effect fires on a non-durable result ──


async def test_no_side_effect_fires_when_required_persistence_fails():
    """Score invalidation and the completion event must not precede durability."""
    coordinator = SideEffectCoordinator()
    with (
        _patch_store(_BoomStore()),
        patch("app.engines.packet_router.get_router") as router,
        patch("app.services.event_emitter.get_emitter") as emitter,
    ):
        router.return_value.notify_score_invalidate = AsyncMock()
        emitter.return_value.emit_enrichment_completed = AsyncMock()
        with pytest.raises(PersistenceRequiredError):
            await coordinator.commit_after_enrich(
                tenant="acme",
                entity_id="res.partner:1",
                object_type="res.partner",
                domain="plastics",
                response_dict=_response(),
                settings=object(),
                idempotency_key="k4",
                require_persistence=True,
            )
        router.return_value.notify_score_invalidate.assert_not_awaited()
        emitter.return_value.emit_enrichment_completed.assert_not_awaited()


# ── 4. The canonical handler refuses to answer `completed` ─


async def test_canonical_converge_reports_failed_when_the_result_is_not_durable():
    from app.engines import handlers
    from app.models.schemas import EnrichResponse

    payload = {
        "entity": {"id": "res.partner:55"},
        "object_type": "res.partner",
        "objective": "enrich",
        "idempotency_key": "k5",
    }
    completed = EnrichResponse(fields={"industry": "plastics"}, state="completed")

    async def _boom(*_a, **_kw):
        raise PersistenceRequiredError("store down")

    with (
        patch.object(handlers, "run_convergence_loop", new_callable=AsyncMock) as loop,
        patch.object(handlers, "_persist_and_sync", _boom),
    ):
        loop.return_value = completed
        result = await handlers.handle_converge("acme", payload)

    assert result["state"] == "failed", "a non-durable result must not read as completed"
    assert "not durable" in result["failure_reason"]
    assert result["fields"] == {}


async def test_canonical_converge_requires_persistence():
    """The canonical branch must ask for durability, not merely tolerate it."""
    from app.engines import handlers
    from app.models.schemas import EnrichResponse

    seen: dict = {}

    async def _record(*_a, **kwargs):
        seen.update(kwargs)

    with (
        patch.object(handlers, "run_convergence_loop", new_callable=AsyncMock) as loop,
        patch.object(handlers, "_persist_and_sync", _record),
    ):
        loop.return_value = EnrichResponse(fields={"x": 1}, state="completed")
        await handlers.handle_converge(
            "acme",
            {"entity": {"id": "res.partner:9"}, "object_type": "res.partner", "objective": "e"},
        )

    assert seen.get("require_persistence") is True
    assert seen.get("graph_sync") is False, "zero synchronous Graph on the canonical path"
