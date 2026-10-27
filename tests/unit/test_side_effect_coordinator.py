"""TASK-021: one persistence/dispatch/event per semantic request.

RV-DOMAIN-RD-07: `graph_synced` is CEG's acknowledgment, not EIE's dispatch.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.side_effect_coordinator import (
    SideEffectCoordinator,
    semantic_side_effect_key,
)

# CEG `handle_sync` success acknowledgment (Cognitive.Engine.Graphs
# engine/handlers.py): status, the endpoint echoed back, rows written.
CEG_SYNC_OK = {"status": "success", "entity_type": "facilities", "synced_count": 1}


def _response() -> dict:
    return {
        "state": "completed",
        "fields": {"polymer_type": "HDPE"},
        "confidence": 0.9,
        "tokens_used": 3,
    }


def _collaborators(graph_ack=CEG_SYNC_OK):
    router = MagicMock()
    router.notify_graph_sync = AsyncMock(return_value=graph_ack)
    router.notify_score_invalidate = AsyncMock()
    emitter = MagicMock()
    emitter.emit_enrichment_completed = AsyncMock()
    return router, emitter


async def _commit(coord, router, emitter, persist=None, **kwargs):
    persist = persist if persist is not None else AsyncMock()
    with (
        patch("app.models.schemas.EnrichResponse.model_validate", return_value=MagicMock()),
        patch("app.services.result_store.ResultStore") as result_store_cls,
        patch("app.engines.packet_router.get_router", return_value=router),
        patch("app.services.event_emitter.get_emitter", return_value=emitter),
    ):
        result_store_cls.return_value.persist_enrich_response = persist
        return await coord.commit_after_enrich(
            tenant=kwargs.pop("tenant", "t1"),
            entity_id=kwargs.pop("entity_id", "e1"),
            object_type="Contact",
            domain="plasticos",
            response_dict=_response(),
            settings=SimpleNamespace(gate_url="https://gate.example"),
            **kwargs,
        )


@pytest.mark.asyncio
async def test_commit_once_per_key() -> None:
    coord = SideEffectCoordinator()
    router, emitter = _collaborators()
    persist = AsyncMock()

    first = await _commit(coord, router, emitter, persist, idempotency_key="idem-1")
    second = await _commit(coord, router, emitter, persist, idempotency_key="idem-1")

    assert first.skipped is False
    assert first.graph_synced is True
    assert second.skipped is True
    assert persist.await_count == 1
    assert router.notify_graph_sync.await_count == 1
    assert router.notify_score_invalidate.await_count == 1
    assert emitter.emit_enrichment_completed.await_count == 1


def test_semantic_key_prefers_packet_id() -> None:
    assert semantic_side_effect_key(
        tenant="t", entity_id="e", packet_id="p-1", idempotency_key="i-1"
    ).startswith("pkt:p-1")


@pytest.mark.asyncio
async def test_graph_sync_false_excludes_only_the_awaited_graph_hop() -> None:
    """EIE-004: `graph_sync` means the Gate->GRAPH round trip, nothing wider.

    The awaited hop to GRAPH is what a latency-bounded caller cannot absorb, so
    that is what the flag excludes. Score invalidation is fire-and-forget to
    SCORE and stays on: suppressing it would leave a stale score on an entity
    that was just enriched, in exchange for no measurable time.
    """
    coord = SideEffectCoordinator()
    router, emitter = _collaborators()

    report = await _commit(coord, router, emitter, idempotency_key="idem-bounded", graph_sync=False)

    assert report.graph_skipped is True
    assert report.graph_synced is False
    assert router.notify_graph_sync.await_count == 0, "the awaited GRAPH hop must be excluded"
    assert report.score_invalidated is True
    assert router.notify_score_invalidate.await_count == 1, (
        "score invalidation is fire-and-forget to SCORE and is deliberately not gated"
    )
    assert report.event_emitted is True

    # Exclusion is a resolved leg, not a failed one: the key completes and the
    # bounded path keeps its once-per-key guard (no graph-retry loop opens).
    again = await _commit(coord, router, emitter, idempotency_key="idem-bounded", graph_sync=False)
    assert again.skipped is True
    assert router.notify_graph_sync.await_count == 0


# ── RV-DOMAIN-RD-07: graph_synced is an acknowledgment ─────────


@pytest.mark.asyncio
async def test_ceg_success_acknowledgment_marks_graph_synced() -> None:
    coord = SideEffectCoordinator()
    router, emitter = _collaborators(graph_ack=dict(CEG_SYNC_OK))

    report = await _commit(coord, router, emitter, idempotency_key="k-ok")

    assert report.graph_synced is True
    assert report.graph_retry is False
    assert not [e for e in report.errors if e.startswith("graph_sync")]
    assert report.key in coord._completed


@pytest.mark.asyncio
async def test_router_none_is_not_a_synchronization() -> None:
    """The reproduction: a mocked Gate failure makes notify_graph_sync return None.

    Before the repair the coordinator set graph_synced=True on the mere return
    of the awaited call, then recorded the key, so the retry that could have
    completed the sync was skipped as a duplicate.
    """
    coord = SideEffectCoordinator()
    router, emitter = _collaborators(graph_ack=None)

    report = await _commit(coord, router, emitter, idempotency_key="k-none")

    assert report.graph_synced is False
    assert report.graph_skipped is False
    assert any(e.startswith("graph_sync:unacknowledged") for e in report.errors)
    assert report.key not in coord._completed, "an unsynced key must not read as completed"
    # Independent effects keep their own semantics: they still ran once.
    assert report.persisted is True
    assert report.score_invalidated is True
    assert report.event_emitted is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ack",
    [
        {"status": "failed", "code": "execution_failed", "error_type": "ExecutionError"},
        {"status": "all_rejected", "entity_type": "facilities", "synced_count": 0},
        {"status": "success", "entity_type": "facilities", "synced_count": 0},
        {"status": "success", "entity_type": "facilities"},
        {"status": "success", "entity_type": "facilities", "synced_count": "1"},
        {"status": "success", "entity_type": "facilities", "synced_count": True},
        {"status": "ok"},
        {"packet_id": "only-transport-metadata"},
        {},
        "success",
        1,
    ],
    ids=[
        "failure-status",
        "all-rejected",
        "zero-synced",
        "missing-synced-count",
        "string-synced-count",
        "bool-synced-count",
        "non-ceg-status",
        "no-status",
        "empty",
        "non-mapping-str",
        "non-mapping-int",
    ],
)
async def test_failed_rejected_or_malformed_acknowledgment_is_not_a_synchronization(ack) -> None:
    coord = SideEffectCoordinator()
    router, emitter = _collaborators(graph_ack=ack)

    report = await _commit(coord, router, emitter, idempotency_key="k-bad")

    assert report.graph_synced is False
    assert any(e.startswith("graph_sync:unacknowledged") for e in report.errors)
    assert report.key not in coord._completed


@pytest.mark.asyncio
async def test_router_exception_is_not_a_synchronization() -> None:
    coord = SideEffectCoordinator()
    router, emitter = _collaborators()
    router.notify_graph_sync = AsyncMock(side_effect=RuntimeError("gate client exploded"))

    report = await _commit(coord, router, emitter, idempotency_key="k-raise")

    assert report.graph_synced is False
    assert any(e == "graph_sync:gate client exploded" for e in report.errors)
    assert report.key not in coord._completed
    assert report.persisted is True
    assert report.event_emitted is True


@pytest.mark.asyncio
async def test_permitted_retry_completes_only_the_failed_graph_leg() -> None:
    """A retry of the same key finishes the sync without duplicating effects."""
    coord = SideEffectCoordinator()
    router, emitter = _collaborators(graph_ack=None)
    persist = AsyncMock()

    first = await _commit(coord, router, emitter, persist, idempotency_key="k-retry")
    assert first.graph_synced is False
    assert first.skipped is False

    # Gate recovers: CEG now acknowledges.
    router.notify_graph_sync = AsyncMock(return_value=dict(CEG_SYNC_OK))
    second = await _commit(coord, router, emitter, persist, idempotency_key="k-retry")

    assert second.skipped is False, "the retry was suppressed as a duplicate"
    assert second.graph_retry is True
    assert second.graph_synced is True
    assert second.persisted is False, "persistence must not run twice"
    assert second.score_invalidated is False
    assert second.event_emitted is False
    assert second.errors == []
    assert second.key in coord._completed

    # Already-completed effects were not duplicated by the retry.
    assert persist.await_count == 1
    assert router.notify_score_invalidate.await_count == 1
    assert emitter.emit_enrichment_completed.await_count == 1
    assert router.notify_graph_sync.await_count == 1, "the recovered router was awaited once"

    # Now the key is complete: a third call is the ordinary duplicate.
    third = await _commit(coord, router, emitter, persist, idempotency_key="k-retry")
    assert third.skipped is True
    assert router.notify_graph_sync.await_count == 1


@pytest.mark.asyncio
async def test_graph_retry_syncs_the_fields_that_were_persisted_not_the_retrying_calls() -> None:
    """The graph must agree with the store and the event, not with a later answer.

    A retry of the same operation (same idempotency key) can carry a different
    answer — the provider re-ran. Its fields must not be what the pending graph
    leg syncs, because PostgreSQL and the completion event hold the first result.

    WU-01 / F-001: this used to rely on the tenant/entity fallback key, under
    which ANY later enrichment of the entity keyed identically — that collapse
    is the defect (a different result is a different operation and persists on
    its own; see test_distinct_results_without_keys_persist_separately). The
    retry now shares the operation's idempotency key, which is what makes it a
    retry; the assertions are unchanged.
    """
    coord = SideEffectCoordinator()
    router, emitter = _collaborators(graph_ack=None)
    persist = AsyncMock()

    first_fields = {"polymer_type": "HDPE", "capacity_tpa": 1200}
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
            response_dict={"state": "completed", "fields": first_fields, "confidence": 0.9},
            settings=SimpleNamespace(gate_url="https://gate.example"),
            idempotency_key="op-1",
        )
        assert first.graph_synced is False

        router.notify_graph_sync = AsyncMock(return_value=dict(CEG_SYNC_OK))
        second = await coord.commit_after_enrich(
            tenant="t1",
            entity_id="e1",
            object_type="Contact",
            domain="other-domain",
            response_dict={
                "state": "completed",
                "fields": {"polymer_type": "PET"},
                "confidence": 0.5,
            },
            settings=SimpleNamespace(gate_url="https://gate.example"),
            idempotency_key="op-1",
        )

    assert second.graph_retry is True
    assert second.graph_synced is True
    sent = router.notify_graph_sync.await_args.kwargs
    assert sent["fields"] == first_fields, "the retry must sync what was persisted"
    assert sent["domain"] == "plasticos"
    assert persist.await_count == 1
    assert emitter.emit_enrichment_completed.await_count == 1


@pytest.mark.asyncio
async def test_retry_that_fails_again_stays_pending_without_duplicating_effects() -> None:
    coord = SideEffectCoordinator()
    router, emitter = _collaborators(graph_ack=None)
    persist = AsyncMock()

    await _commit(coord, router, emitter, persist, idempotency_key="k-again")
    second = await _commit(coord, router, emitter, persist, idempotency_key="k-again")

    assert second.graph_retry is True
    assert second.graph_synced is False
    assert second.skipped is False
    assert second.key not in coord._completed
    assert router.notify_graph_sync.await_count == 2
    assert persist.await_count == 1
    assert router.notify_score_invalidate.await_count == 1
    assert emitter.emit_enrichment_completed.await_count == 1


@pytest.mark.asyncio
async def test_graph_sync_false_on_a_pending_key_keeps_the_leg_pending() -> None:
    """A call that excludes the graph leg cannot resolve a pending one (F-234-1).

    The canonical converge path calls with graph_sync=False; under the
    tenant/entity fallback it can key identically to an earlier handle_enrich
    call whose graph leg CEG never acknowledged. Excluding the leg is not
    acknowledging it: the key must stay pending with an explicit error, so the
    next permitted call that does ask for the graph leg can still finish it.
    """
    coord = SideEffectCoordinator()
    router, emitter = _collaborators(graph_ack=None)
    persist = AsyncMock()

    first = await _commit(coord, router, emitter, persist, idempotency_key="k-excl")
    assert first.graph_synced is False
    assert first.key in coord._graph_pending

    excluded = await _commit(
        coord, router, emitter, persist, idempotency_key="k-excl", graph_sync=False
    )

    assert excluded.graph_retry is True
    assert excluded.graph_skipped is True
    assert excluded.graph_synced is False
    assert excluded.skipped is False
    assert any(e.startswith("graph_sync:excluded_while_pending") for e in excluded.errors)
    assert excluded.key in coord._graph_pending, "the unacknowledged graph leg was dropped"
    assert excluded.key not in coord._completed
    # Nothing ran twice, and the excluded leg was not attempted.
    assert router.notify_graph_sync.await_count == 1
    assert persist.await_count == 1
    assert router.notify_score_invalidate.await_count == 1
    assert emitter.emit_enrichment_completed.await_count == 1

    # A later call that asks for the graph leg completes it.
    router.notify_graph_sync = AsyncMock(return_value=dict(CEG_SYNC_OK))
    finished = await _commit(coord, router, emitter, persist, idempotency_key="k-excl")
    assert finished.graph_retry is True
    assert finished.graph_synced is True
    assert finished.key in coord._completed
    assert finished.key not in coord._graph_pending


@pytest.mark.asyncio
async def test_graph_retry_syncs_to_the_entity_that_was_persisted_not_the_retrying_calls() -> None:
    """The pending graph leg pins its entity, like its fields and domain (F-234-3).

    An idem:{tenant}:{idempotency_key} key excludes entity_id, so a caller that
    reuses an idempotency key across entities keys e2's call as a retry of e1's.
    That retry must write e1's fields to e1 — never e1's fields onto e2 — and
    record the mismatch rather than report a clean sync for e2.
    """
    coord = SideEffectCoordinator()
    router, emitter = _collaborators(graph_ack=None)
    persist = AsyncMock()

    first = await _commit(
        coord, router, emitter, persist, entity_id="e1", idempotency_key="k-shared"
    )
    assert first.graph_synced is False
    assert coord._graph_pending[first.key]["entity_id"] == "e1"

    router.notify_graph_sync = AsyncMock(return_value=dict(CEG_SYNC_OK))
    second = await _commit(
        coord, router, emitter, persist, entity_id="e2", idempotency_key="k-shared"
    )

    assert second.graph_retry is True
    sent = router.notify_graph_sync.await_args.kwargs
    assert sent["entity_id"] == "e1", "the retry must sync the entity that was persisted"
    assert sent["fields"] == _response()["fields"]
    assert router.notify_graph_sync.await_count == 1
    assert any(e.startswith("graph_sync:entity_mismatch") for e in second.errors)
    assert second.graph_synced is True
    assert second.key in coord._completed
    # e2 was never persisted or emitted under e1's key.
    assert persist.await_count == 1
    assert persist.await_args.kwargs["entity_id"] == "e1"
    assert emitter.emit_enrichment_completed.await_count == 1


@pytest.mark.asyncio
async def test_pending_graph_leg_is_per_key_and_per_tenant() -> None:
    """A pending key never leaks into another tenant's or entity's key."""
    coord = SideEffectCoordinator()
    router, emitter = _collaborators(graph_ack=None)
    persist = AsyncMock()

    await _commit(coord, router, emitter, persist, tenant="t1", idempotency_key="shared")
    other = await _commit(coord, router, emitter, persist, tenant="t2", idempotency_key="shared")

    assert other.graph_retry is False, "t2's first call is not a retry of t1's key"
    assert other.persisted is True
    assert persist.await_count == 2
    assert semantic_side_effect_key(
        tenant="t1", entity_id="e1", idempotency_key="shared"
    ) != semantic_side_effect_key(tenant="t2", entity_id="e1", idempotency_key="shared")


@pytest.mark.asyncio
async def test_reset_clears_pending_graph_legs() -> None:
    coord = SideEffectCoordinator()
    router, emitter = _collaborators(graph_ack=None)
    await _commit(coord, router, emitter, idempotency_key="k-reset")
    assert coord._graph_pending
    coord.reset_for_tests()
    assert not coord._graph_pending
    assert not coord._completed


# ── GRAPH-IDENT-02: the entity's node label reaches the router ──────────────


@pytest.mark.asyncio
async def test_node_label_is_passed_to_the_router() -> None:
    """The router selects the domain's CEG endpoint by it; the coordinator forwards it."""
    coord = SideEffectCoordinator()
    router, emitter = _collaborators()
    report = await _commit(coord, router, emitter, idempotency_key="k-label", node_label="Facility")
    assert report.graph_synced is True
    assert router.notify_graph_sync.await_args.kwargs["node_label"] == "Facility"


@pytest.mark.asyncio
async def test_graph_retry_resends_the_first_attempts_node_label() -> None:
    """Like the fields and domain it pins, the pending graph leg keeps its label."""
    coord = SideEffectCoordinator()
    router, emitter = _collaborators(graph_ack=None)
    persist = AsyncMock()

    first = await _commit(
        coord, router, emitter, persist, idempotency_key="k-label-retry", node_label="Facility"
    )
    assert first.graph_synced is False
    assert coord._graph_pending[first.key]["node_label"] == "Facility"

    router.notify_graph_sync = AsyncMock(return_value=dict(CEG_SYNC_OK))
    second = await _commit(
        coord, router, emitter, persist, idempotency_key="k-label-retry", node_label="Other"
    )
    assert second.graph_retry is True
    assert second.graph_synced is True
    assert router.notify_graph_sync.await_args.kwargs["node_label"] == "Facility"


# ── WU-01: operation-scoped identity and atomic reservation ───────────────


def _result(fields: dict) -> dict:
    return {"state": "completed", "fields": fields, "confidence": 0.9, "tokens_used": 3}


def _effects(persist):
    """Patch the coordinator's collaborators once (patch contexts must not overlap)."""
    from contextlib import ExitStack

    router, emitter = _collaborators()
    stack = ExitStack()
    stack.enter_context(
        patch("app.models.schemas.EnrichResponse.model_validate", side_effect=lambda d: d)
    )
    result_store_cls = stack.enter_context(patch("app.services.result_store.ResultStore"))
    result_store_cls.return_value.persist_enrich_response = persist
    stack.enter_context(patch("app.engines.packet_router.get_router", return_value=router))
    stack.enter_context(patch("app.services.event_emitter.get_emitter", return_value=emitter))
    return stack


def _raw_commit(coord, response_dict, **kwargs):
    return coord.commit_after_enrich(
        tenant="t1",
        entity_id="e1",
        object_type="Account",
        domain="plasticos",
        response_dict=response_dict,
        settings=SimpleNamespace(gate_url="https://gate.example"),
        graph_sync=False,
        **kwargs,
    )


async def _commit_result(coord, persist, response_dict, **kwargs):
    with _effects(persist):
        return await _raw_commit(coord, response_dict, **kwargs)


@pytest.mark.asyncio
async def test_distinct_results_without_keys_persist_separately() -> None:
    """F-001: two converges of one entity, different results, no keys -> two persists."""
    coord = SideEffectCoordinator()
    persist = AsyncMock()

    first = await _commit_result(coord, persist, _result({"industry": "Recycling"}))
    second = await _commit_result(coord, persist, _result({"industry": "Plastics"}))

    assert first.persisted is True
    assert second.persisted is True
    assert second.skipped is False
    persisted_fields = [c.kwargs["response"]["fields"] for c in persist.await_args_list]
    assert persisted_fields == [{"industry": "Recycling"}, {"industry": "Plastics"}]


@pytest.mark.asyncio
async def test_replayed_packet_is_skipped_and_persists_once() -> None:
    """F-001: the identical packet (same header packet_id) replayed is a duplicate."""
    coord = SideEffectCoordinator()
    persist = AsyncMock()

    await _commit_result(coord, persist, _result({"industry": "Recycling"}), packet_id="p-1")
    replay = await _commit_result(
        coord, persist, _result({"industry": "Recycling"}), packet_id="p-1"
    )

    assert replay.skipped is True
    assert persist.await_count == 1


@pytest.mark.asyncio
async def test_gather_of_identical_commits_persists_once() -> None:
    """F-024: the key is reserved before the first await."""
    import asyncio

    coord = SideEffectCoordinator()

    async def slow_persist(**kwargs):
        await asyncio.sleep(0.01)

    persist = AsyncMock(side_effect=slow_persist)
    with _effects(persist):
        reports = await asyncio.gather(
            _raw_commit(coord, _result({"x": 1}), idempotency_key="k"),
            _raw_commit(coord, _result({"x": 1}), idempotency_key="k"),
        )

    assert persist.await_count == 1
    assert sorted(r.skipped for r in reports) == [False, True]


@pytest.mark.asyncio
async def test_concurrent_duplicate_of_a_failed_required_persist_never_reads_completed() -> None:
    """F-001: a waiter whose holder failed re-evaluates; it does not skip as done."""
    import asyncio

    from app.services.side_effect_coordinator import PersistenceRequiredError

    coord = SideEffectCoordinator()

    async def failing_persist(**kwargs):
        await asyncio.sleep(0.01)
        raise ConnectionError("store down")

    persist = AsyncMock(side_effect=failing_persist)
    with _effects(persist):
        results = await asyncio.gather(
            _raw_commit(coord, _result({"x": 1}), idempotency_key="k", require_persistence=True),
            _raw_commit(coord, _result({"x": 1}), idempotency_key="k", require_persistence=True),
            return_exceptions=True,
        )

    assert all(isinstance(r, PersistenceRequiredError) for r in results)
    # The waiter re-attempted under its own reservation instead of skipping.
    assert persist.await_count == 2
    assert "idem:t1:k:enrich" not in coord._completed


@pytest.mark.asyncio
async def test_required_persistence_refuses_a_key_completed_without_a_durable_result() -> None:
    """F-001: no path answers 'completed' for a skipped, never-persisted result."""
    from app.services.side_effect_coordinator import PersistenceRequiredError

    coord = SideEffectCoordinator()
    failing = AsyncMock(side_effect=ConnectionError("store down"))

    # Fire-and-forward attempt: persistence not required, it fails, key completes.
    first = await _commit_result(coord, failing, _result({"x": 1}), idempotency_key="k")
    assert first.persisted is False

    with pytest.raises(PersistenceRequiredError):
        await _commit_result(
            coord, AsyncMock(), _result({"x": 1}), idempotency_key="k", require_persistence=True
        )


def test_result_digest_separates_results_but_not_identical_ones() -> None:
    from app.services.side_effect_coordinator import result_digest

    a = semantic_side_effect_key(
        tenant="t", entity_id="e", result_digest=result_digest(_result({"x": 1}))
    )
    b = semantic_side_effect_key(
        tenant="t", entity_id="e", result_digest=result_digest(_result({"x": 2}))
    )
    a2 = semantic_side_effect_key(
        tenant="t", entity_id="e", result_digest=result_digest(_result({"x": 1}))
    )
    assert a != b
    assert a == a2
