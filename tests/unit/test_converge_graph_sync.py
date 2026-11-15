"""A completed canonical converge result reaches CEG, detached from its answer.

Operator decision 2026-11-15: converge results go to the graph straight away;
human review happens only before results are merged into the producer. The
canonical converge path still makes no AWAITED Graph call (E14): the graph leg
is deferred by the side-effect coordinator and started only after the answer is
decided `completed`, outside the request deadline.

What these tests pin:

* a completed converge schedules exactly one graph sync, carrying only the
  admitted fields of the identity converge resolved;
* the answer is returned while that sync is still blocked;
* failed, degraded and non-durable converges schedule nothing;
* a sync failure leaves the answer `completed` and the key pending for its
  graph leg; a later leg can finish it;
* a retry of the same operation never double-syncs;
* `converge_graph_sync_enabled=False` schedules nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import get_settings
from app.engines.handlers import admitted_fields, handle_converge
from app.models.schemas import EnrichResponse
from app.services.request_deadline import current_deadline
from app.services.side_effect_coordinator import get_side_effect_coordinator

pytestmark = pytest.mark.unit

TENANT = "tenant-a"
ENTITY_ID = "entity:1"
ACK = {"status": "success", "entity_type": "entity", "synced_count": 1}


def _request(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "entity": {"id": ENTITY_ID, "name": "Example"},
        "object_type": "entity",
        "objective": "enrich",
        "schema": {"website": "string", "size": "integer"},
    }
    body.update(overrides)
    return body


def _completed(**overrides: Any) -> EnrichResponse:
    base: dict[str, Any] = {
        # `unrequested` is outside the request schema: the v1 cut drops it,
        # so it is never admitted and never synced.
        "fields": {"website": "https://example.test", "size": 12, "unrequested": "x"},
        "confidence": 0.9,
        "pass_count": 1,
        "state": "completed",
    }
    base.update(overrides)
    return EnrichResponse(**base)


@pytest.fixture
def probes() -> Iterator[dict[str, Any]]:
    coordinator = get_side_effect_coordinator()
    coordinator.reset_for_tests()
    with (
        patch("app.engines.handlers.run_convergence_loop", new_callable=AsyncMock) as loop,
        patch("app.services.result_store.ResultStore.persist_enrich_response") as persist,
        patch("app.engines.packet_router.get_router") as router_factory,
        patch("app.services.event_emitter.get_emitter") as emitter_factory,
    ):
        loop.return_value = _completed()
        persist.return_value = None
        router = router_factory.return_value
        router.notify_graph_sync = AsyncMock(return_value=ACK)
        router.notify_score_invalidate = AsyncMock(return_value=None)
        emitter_factory.return_value.emit_enrichment_completed = AsyncMock(return_value=None)
        yield {"loop": loop, "persist": persist, "router": router, "coordinator": coordinator}
    coordinator.reset_for_tests()


def _pending_keys(coordinator: Any) -> list[str]:
    return list(coordinator._graph_pending)


async def test_completed_converge_syncs_once_with_only_admitted_fields(probes):
    result = await handle_converge(TENANT, _request())
    await probes["coordinator"].drain_graph_legs()

    assert result["state"] == "completed"
    sync = probes["router"].notify_graph_sync
    assert sync.await_count == 1
    kwargs = sync.await_args.kwargs
    assert kwargs["tenant_id"] == TENANT
    assert kwargs["entity_id"] == ENTITY_ID
    assert kwargs["fields"] == {"website": "https://example.test", "size": 12}
    assert _pending_keys(probes["coordinator"]) == [], "an acknowledged sync completes the key"


async def test_answer_is_returned_while_the_sync_is_still_blocked(probes):
    release = asyncio.Event()
    started = asyncio.Event()
    deadlines: list[Any] = []

    async def _blocking_sync(**_kwargs: Any) -> dict[str, Any]:
        deadlines.append(current_deadline())
        started.set()
        await release.wait()
        return ACK

    probes["router"].notify_graph_sync = AsyncMock(side_effect=_blocking_sync)

    result = await asyncio.wait_for(handle_converge(TENANT, _request()), timeout=5.0)

    assert result["state"] == "completed", "the answer did not wait for the graph"
    await asyncio.wait_for(started.wait(), timeout=5.0)
    assert probes["router"].notify_graph_sync.await_count == 1
    assert deadlines == [None], "the detached leg must not run under the request deadline"
    assert _pending_keys(probes["coordinator"]), "unacknowledged until CEG answers"

    release.set()
    await probes["coordinator"].drain_graph_legs()
    assert _pending_keys(probes["coordinator"]) == []


@pytest.mark.parametrize("state", ["failed", "degraded"])
async def test_non_completed_converge_schedules_no_sync(probes, state):
    probes["loop"].return_value = EnrichResponse(state=state, failure_reason="r", fields={})

    result = await handle_converge(TENANT, _request())
    await probes["coordinator"].drain_graph_legs()

    assert result["state"] == state
    probes["router"].notify_graph_sync.assert_not_awaited()
    assert _pending_keys(probes["coordinator"]) == []


async def test_non_durable_converge_schedules_no_sync(probes):
    probes["persist"].side_effect = RuntimeError("store down")

    result = await handle_converge(TENANT, _request())
    await probes["coordinator"].drain_graph_legs()

    assert result["state"] == "failed"
    probes["router"].notify_graph_sync.assert_not_awaited()


async def test_converge_that_times_out_after_persisting_schedules_no_sync(probes):
    """A degraded answer is decided by the deadline; nothing is synced for it."""

    async def _slow_score(**_kwargs: Any) -> None:
        await asyncio.sleep(5.0)

    probes["router"].notify_score_invalidate = AsyncMock(side_effect=_slow_score)
    from app.engines import handlers

    result = await handlers._handle_canonical_converge(TENANT, _request(), budget_seconds=1.0)
    await probes["coordinator"].drain_graph_legs()

    assert result["state"] == "degraded"
    probes["router"].notify_graph_sync.assert_not_awaited()


async def test_sync_failure_leaves_answer_completed_and_key_pending(probes):
    probes["router"].notify_graph_sync = AsyncMock(side_effect=RuntimeError("gate down"))

    result = await handle_converge(TENANT, _request(idempotency_key="op-1"))
    await probes["coordinator"].drain_graph_legs()

    assert result["state"] == "completed"
    assert probes["router"].notify_graph_sync.await_count == 1
    pending = _pending_keys(probes["coordinator"])
    assert len(pending) == 1, "the graph leg stays pending for a later attempt"

    # A retry of the same operation runs the graph leg only, and finishes it.
    probes["router"].notify_graph_sync = AsyncMock(return_value=ACK)
    retry = await handle_converge(TENANT, _request(idempotency_key="op-1"))
    await probes["coordinator"].drain_graph_legs()

    assert retry["state"] == "completed"
    assert probes["router"].notify_graph_sync.await_count == 1
    assert probes["persist"].call_count == 1, "the retry must not persist again"
    assert _pending_keys(probes["coordinator"]) == []


async def test_retry_of_the_same_operation_never_double_syncs(probes):
    release = asyncio.Event()

    async def _blocking_sync(**_kwargs: Any) -> dict[str, Any]:
        await release.wait()
        return ACK

    probes["router"].notify_graph_sync = AsyncMock(side_effect=_blocking_sync)

    await handle_converge(TENANT, _request(idempotency_key="op-2"))
    # Same operation while its leg is still in flight: no second sync.
    await handle_converge(TENANT, _request(idempotency_key="op-2"))
    release.set()
    await probes["coordinator"].drain_graph_legs()
    # Same operation after the acknowledged sync: a duplicate, no second sync.
    await handle_converge(TENANT, _request(idempotency_key="op-2"))
    await probes["coordinator"].drain_graph_legs()

    assert probes["router"].notify_graph_sync.await_count == 1
    assert probes["persist"].call_count == 1


async def test_flag_off_schedules_no_sync(probes, monkeypatch):
    monkeypatch.setattr(get_settings(), "converge_graph_sync_enabled", False)

    result = await handle_converge(TENANT, _request())
    await probes["coordinator"].drain_graph_legs()

    assert result["state"] == "completed"
    probes["router"].notify_graph_sync.assert_not_awaited()
    assert _pending_keys(probes["coordinator"]) == []


def test_v2_admitted_fields_are_exactly_the_admitted_entries():
    body = {
        "fields": {"website": "https://example.test"},
        "field_evidence": {
            "website": {"state": "inferred", "admitted": True, "value": "https://example.test"},
            "size": {"state": "external_claim", "admitted": False, "value": 3},
            "seen": {"state": "observed", "admitted": False, "value": "obs"},
            "gap": {"state": "unknown", "admitted": False, "confidence": None},
        },
    }

    assert admitted_fields(body) == {"website": "https://example.test"}


def test_v1_admitted_fields_are_the_fields():
    assert admitted_fields({"fields": {"a": 1}}) == {"a": 1}
    assert admitted_fields({"fields": None}) == {}


async def test_sync_row_omits_unset_and_null_fields_never_sends_null(probes):
    """CEG writes `SET n += row.node`: a null key deletes the graph property.

    A completed converge whose admitted fields leave declared row properties
    unset (`email`) or valueless (`size: None`) must produce a row with those
    keys ABSENT, so the graph keeps what it holds.
    """
    from app.engines.packet_router import GraphSyncContract, project_graph_sync_row

    probes["loop"].return_value = _completed(
        fields={"website": "https://example.test", "size": None}
    )

    result = await handle_converge(TENANT, _request())
    await probes["coordinator"].drain_graph_legs()

    assert result["state"] == "completed"
    fields = probes["router"].notify_graph_sync.await_args.kwargs["fields"]
    contract = GraphSyncContract(
        entity_type="entity",
        id_property="entity_id",
        source="test",
        row_schema={
            "entity_id": {"type": "string"},
            "website": {"type": "string"},
            "size": {"type": "int"},
            "email": {"type": "string"},
        },
    )
    row = project_graph_sync_row(entity_id=ENTITY_ID, fields=fields, contract=contract).row

    assert row == {"entity_id": ENTITY_ID, "website": "https://example.test"}
    assert None not in row.values()


def test_admitted_fields_never_carry_a_null():
    v2 = {
        "field_evidence": {
            "a": {"state": "inferred", "admitted": True, "value": 1},
            "b": {"state": "inferred", "admitted": True, "value": None},
        }
    }
    assert admitted_fields(v2) == {"a": 1}
    assert admitted_fields({"fields": {"a": 1, "b": None}}) == {"a": 1}
