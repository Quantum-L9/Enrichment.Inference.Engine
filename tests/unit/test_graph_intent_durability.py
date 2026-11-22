"""A2: the graph leg of a committed result is a durable obligation.

`SideEffectCoordinator` records the graph intent inside the result's own
persistence call (`pg_store.graph_intent_scope`), sends it under a lease, and
settles each attempt as acknowledged / retry / terminal. A fresh coordinator --
a restarted process -- recovers what the old one left with its pinned input.

The store here is an in-memory stand-in with `pg_store`'s claim/settle
semantics; `tests/integration/test_graph_intent_store_pg.py` proves the same
semantics on PostgreSQL (atomicity, leases across workers, version order).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.engines.packet_router import GraphSyncOutcome, GraphSyncOutcomeKind, GraphSyncPin
from app.services import pg_store
from app.services.side_effect_coordinator import SideEffectCoordinator

TENANT = "synthco"
ACK = {"status": "success", "entity_type": "depots", "synced_count": 1}
PIN = GraphSyncPin(entity_type="depots", contract_digest="c" * 64)
SETTINGS = SimpleNamespace(gate_url="https://gate.example")


@dataclass
class _Row:
    id: uuid.UUID
    seq: int
    tenant_id: str
    effect_key: str
    entity_id: str
    node_label: str | None
    domain: str | None
    endpoint: str | None
    contract_digest: str | None
    fields: dict[str, Any]
    state: str = "pending"
    attempts: int = 0
    max_attempts: int = 8
    lease_owner: str | None = None
    lease_expires_at: Any = None
    due: bool = True
    origin: str = "enrich"
    enrichment_result_id: uuid.UUID | None = None
    last_error: str | None = None
    last_ack: dict[str, Any] | None = None
    cartridge_revision: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state in pg_store.GRAPH_INTENT_TERMINAL_STATES


@dataclass
class FakeIntents:
    """`pg_store` graph-intent semantics, in memory, shared across 'processes'."""

    rows: dict[tuple[str, str], _Row] = field(default_factory=dict)
    settles: list[tuple[str, str]] = field(default_factory=list)

    def record(self, spec: pg_store.GraphIntentSpec) -> None:
        key = (spec.tenant_id, spec.effect_key)
        if key in self.rows:
            raise pg_store.GraphIntentDuplicateError(spec.tenant_id, spec.effect_key)
        self.rows[key] = _Row(
            id=uuid.uuid4(),
            seq=len(self.rows) + 1,
            tenant_id=spec.tenant_id,
            effect_key=spec.effect_key,
            entity_id=spec.entity_id,
            node_label=spec.node_label,
            domain=spec.domain,
            endpoint=spec.endpoint,
            contract_digest=spec.contract_digest,
            fields=dict(spec.fields),
            max_attempts=spec.max_attempts,
            origin=spec.origin,
            enrichment_result_id=uuid.uuid4(),
        )

    configured_store: bool = True
    claims: int = 0

    def configured(self) -> bool:
        return self.configured_store

    def _lease(self, row: _Row, owner: str) -> _Row:
        self.claims += 1
        row.state = "dispatching"
        row.lease_owner = f"{owner}#{self.claims}"  # a fresh fencing token per claim
        row.attempts += 1
        return replace(row)

    def _claimable(self, row: _Row, held: frozenset[str] = frozenset()) -> bool:
        return (
            row.state == "pending"
            and row.due
            and row.attempts < row.max_attempts
            and row.origin not in held
        )

    async def claim(self, *, owner: str, tenant_id: str, effect_key: str) -> list[Any]:
        row = self.rows.get((tenant_id, effect_key))
        return [self._lease(row, owner)] if row and self._claimable(row) else []

    async def claim_due(
        self, *, owner: str, limit: int, held_origins: frozenset[str] = frozenset()
    ) -> list[Any]:
        due = [r for r in self.rows.values() if self._claimable(r, held_origins)][:limit]
        return [self._lease(r, owner) for r in due]

    async def get(self, tenant_id: str, effect_key: str) -> Any | None:
        row = self.rows.get((tenant_id, effect_key))
        return replace(row) if row else None

    async def settle(
        self, intent_id: Any, *, owner: str, outcome: str, error: str | None, ack: Any
    ) -> str | None:
        row = next(r for r in self.rows.values() if r.id == intent_id)
        if row.state != "dispatching" or row.lease_owner != owner:
            return None
        row.lease_owner = None
        self.settles.append((row.effect_key, outcome))
        if outcome == "acknowledged":
            row.state, row.last_ack = "acknowledged", dict(ack or {})
        elif outcome == "terminal":
            row.state, row.last_error = "failed_terminal", error
        elif row.attempts >= row.max_attempts:
            row.state, row.last_error = "failed_terminal", f"retry_exhausted:{error}"
        else:
            row.state, row.last_error = "pending", error
        return row.state

    def only(self) -> _Row:
        (row,) = self.rows.values()
        return row


def _router(*answers: Any) -> MagicMock:
    """A router whose `notify_graph_sync` answers in turn and reports each outcome."""
    queue = list(answers)

    async def notify(**kwargs: Any) -> Any:
        answer = queue.pop(0) if queue else None
        on_outcome = kwargs["on_outcome"]
        if isinstance(answer, GraphSyncOutcome):
            on_outcome(answer)
            return answer.ack
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            on_outcome(
                GraphSyncOutcome(GraphSyncOutcomeKind.RETRYABLE, "route:NodeUnreachableError")
            )
            return None
        on_outcome(GraphSyncOutcome(GraphSyncOutcomeKind.ACKNOWLEDGED, "acknowledged", answer))
        return answer

    router = MagicMock()
    router.notify_graph_sync = AsyncMock(side_effect=notify)
    router.graph_sync_pin = MagicMock(return_value=PIN)
    return router


@dataclass
class _World:
    intents: FakeIntents
    persist: AsyncMock
    emitter: MagicMock


def _world() -> _World:
    intents = FakeIntents()

    async def persist(**_: Any) -> uuid.UUID:
        spec = pg_store._graph_intent_var.get()
        if spec is not None:
            intents.record(spec)  # same "transaction" as the result
        return uuid.uuid4()

    emitter = MagicMock()
    emitter.emit_enrichment_completed = AsyncMock()
    return _World(intents=intents, persist=AsyncMock(side_effect=persist), emitter=emitter)


async def _commit(
    coord: SideEffectCoordinator, world: _World, router: MagicMock, **kwargs: Any
) -> Any:
    with (
        patch("app.models.schemas.EnrichResponse.model_validate", return_value=MagicMock()),
        patch("app.services.result_store.ResultStore") as store_cls,
        patch("app.engines.packet_router.get_router", return_value=router),
        patch("app.services.event_emitter.get_emitter", return_value=world.emitter),
    ):
        store_cls.return_value.persist_enrich_response = world.persist
        return await coord.commit_after_enrich(
            tenant=TENANT,
            entity_id=kwargs.pop("entity_id", "d-1"),
            object_type="Depot",
            domain=TENANT,
            response_dict={
                "state": "completed",
                "fields": kwargs.pop("fields", {"capacity": 2.5}),
                "confidence": 0.9,
            },
            settings=SETTINGS,
            node_label="Depot",
            **kwargs,
        )


async def _recover(coord: SideEffectCoordinator, router: MagicMock) -> int:
    with patch("app.engines.packet_router.get_router", return_value=router):
        return await coord.recover_graph_intents(SETTINGS)


# ── P14: completion is independent of graph availability ────────────────────


@pytest.mark.asyncio
async def test_deferred_leg_is_durable_at_completion_with_pinned_input() -> None:
    world = _world()
    coord = SideEffectCoordinator(intents=world.intents)
    router = _router()
    report = await _commit(
        coord,
        world,
        router,
        idempotency_key="op-1",
        graph_sync=False,
        defer_graph_sync=True,
        require_persistence=True,
        graph_fields={"capacity": 2.5},
    )
    assert report.persisted and report.graph_durable and report.graph_deferred
    router.notify_graph_sync.assert_not_awaited()  # nothing awaited on the answer's path
    row = world.intents.only()
    assert row.state == "pending"
    assert (row.tenant_id, row.entity_id, row.node_label) == (TENANT, "d-1", "Depot")
    assert row.fields == {"capacity": 2.5}
    assert (row.endpoint, row.contract_digest) == (PIN.entity_type, PIN.contract_digest)


@pytest.mark.asyncio
async def test_ceg_down_keeps_completion_and_leaves_the_intent_retryable() -> None:
    world = _world()
    coord = SideEffectCoordinator(intents=world.intents)
    report = await _commit(coord, world, _router(None), idempotency_key="op-1")
    assert report.persisted and report.event_emitted and not report.graph_synced
    assert not report.graph_terminal
    assert world.intents.only().state == "pending"
    assert world.intents.settles == [(report.key, "retry")]


# ── P15: restart before dispatch ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restart_before_dispatch_recovers_and_sends_the_pinned_values() -> None:
    world = _world()
    first_life = SideEffectCoordinator(intents=world.intents)
    await _commit(
        first_life,
        world,
        _router(),
        idempotency_key="op-1",
        graph_sync=False,
        defer_graph_sync=True,
        graph_fields={"capacity": 2.5},
    )
    # The process dies before `schedule_graph_leg`; a new one starts empty.
    second_life = SideEffectCoordinator(intents=world.intents)
    router = _router(ACK)
    assert await _recover(second_life, router) == 1
    sent = router.notify_graph_sync.await_args.kwargs
    assert (sent["tenant_id"], sent["entity_id"], sent["node_label"]) == (TENANT, "d-1", "Depot")
    assert sent["fields"] == {"capacity": 2.5}
    assert sent["expected_contract_digest"] == PIN.contract_digest
    assert world.intents.only().state == "acknowledged"
    assert world.persist.await_count == 1
    assert world.emitter.emit_enrichment_completed.await_count == 1


# ── P16: CEG wrote, the ack was lost, EIE restarts ──────────────────────────


@pytest.mark.asyncio
async def test_lost_ack_then_restart_replays_once_without_duplicate_effects() -> None:
    world = _world()
    first_life = SideEffectCoordinator(intents=world.intents)
    lost = _router(None)  # CEG wrote; the answer never arrived
    await _commit(first_life, world, lost, idempotency_key="op-1")
    first_sent = lost.notify_graph_sync.await_args.kwargs

    second_life = SideEffectCoordinator(intents=world.intents)
    replay = _router(ACK)
    await _recover(second_life, replay)
    replay_sent = replay.notify_graph_sync.await_args.kwargs
    for name in ("tenant_id", "entity_id", "fields", "node_label", "expected_contract_digest"):
        assert replay_sent[name] == first_sent[name], name  # same row -> same MERGE/key
    assert world.intents.only().state == "acknowledged"
    assert world.persist.await_count == 1
    assert world.emitter.emit_enrichment_completed.await_count == 1
    # A caller retrying the operation in yet another fresh process is a durable
    # duplicate: its commit rolls back on the intent key, and nothing re-runs.
    third_life = SideEffectCoordinator(intents=world.intents)
    third = await _commit(third_life, world, _router(ACK), idempotency_key="op-1")
    assert third.skipped is True
    assert world.persist.await_count == 2  # attempted, rolled back as duplicate
    assert world.emitter.emit_enrichment_completed.await_count == 1


# ── P17: refusal is terminal, accurate, never replayed ──────────────────────


@pytest.mark.parametrize("reason", ["row_type", "contract_changed", "no_contract", "all_rejected"])
@pytest.mark.asyncio
async def test_refusal_is_terminal_and_never_replayed(reason: str) -> None:
    world = _world()
    coord = SideEffectCoordinator(intents=world.intents)
    refused = GraphSyncOutcome(GraphSyncOutcomeKind.REFUSED, reason)
    router = _router(refused)
    report = await _commit(coord, world, router, idempotency_key="op-1")
    assert report.graph_terminal and not report.graph_synced
    assert f"graph_sync:refused:{reason}" in report.errors
    assert world.intents.only().state == "failed_terminal"
    assert await _recover(SideEffectCoordinator(intents=world.intents), _router(ACK)) == 0
    again = await _commit(coord, world, router, idempotency_key="op-1")
    assert again.skipped is True
    assert router.notify_graph_sync.await_count == 1


# ── P20: same-key concurrency, and a later legitimate version ───────────────


@pytest.mark.asyncio
async def test_two_simultaneous_same_key_completions_send_once() -> None:
    """The second call arrives while the first holds the key (forced overlap)."""
    world = _world()
    coord = SideEffectCoordinator(intents=world.intents)
    router = _router(ACK, ACK)
    release = asyncio.Event()
    entered = asyncio.Event()
    record = world.persist.side_effect

    async def blocking_persist(**kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return await record(**kwargs)

    world.persist.side_effect = blocking_persist
    with (
        patch("app.models.schemas.EnrichResponse.model_validate", return_value=MagicMock()),
        patch("app.services.result_store.ResultStore") as store_cls,
        patch("app.engines.packet_router.get_router", return_value=router),
        patch("app.services.event_emitter.get_emitter", return_value=world.emitter),
    ):
        store_cls.return_value.persist_enrich_response = world.persist

        def call() -> Any:
            return coord.commit_after_enrich(
                tenant=TENANT,
                entity_id="d-1",
                object_type="Depot",
                domain=TENANT,
                response_dict={"state": "completed", "fields": {"capacity": 2.5}},
                settings=SETTINGS,
                node_label="Depot",
                idempotency_key="op-1",
            )

        first = asyncio.create_task(call())
        await entered.wait()
        second = asyncio.create_task(call())
        await asyncio.sleep(0)
        assert world.persist.await_count == 1, "the second call must wait on the first"
        release.set()
        a, b = await asyncio.gather(first, second)
    assert sorted([a.skipped, b.skipped]) == [False, True]
    assert world.persist.await_count == 1
    assert router.notify_graph_sync.await_count == 1
    assert world.emitter.emit_enrichment_completed.await_count == 1


@pytest.mark.asyncio
async def test_same_key_from_another_worker_is_a_durable_duplicate() -> None:
    world = _world()
    worker_a = SideEffectCoordinator(intents=world.intents)
    worker_b = SideEffectCoordinator(intents=world.intents)
    await _commit(worker_a, world, _router(ACK), idempotency_key="op-1")
    router_b = _router(ACK)
    dup = await _commit(worker_b, world, router_b, idempotency_key="op-1")
    assert dup.skipped is True
    router_b.notify_graph_sync.assert_not_awaited()
    assert world.emitter.emit_enrichment_completed.await_count == 1


@pytest.mark.asyncio
async def test_a_later_legitimate_version_is_recorded_and_sent() -> None:
    world = _world()
    coord = SideEffectCoordinator(intents=world.intents)
    router = _router(ACK, ACK)
    await _commit(coord, world, router, idempotency_key="op-1", fields={"capacity": 1.0})
    newer = await _commit(coord, world, router, idempotency_key="op-2", fields={"capacity": 2.0})
    assert newer.graph_synced is True
    assert router.notify_graph_sync.await_args.kwargs["fields"] == {"capacity": 2.0}
    assert {r.state for r in world.intents.rows.values()} == {"acknowledged"}


# ── P21: bounded retries, terminal failure observable ───────────────────────


@pytest.mark.asyncio
async def test_spent_retries_end_terminal_and_never_claim_synced() -> None:
    world = _world()
    coord = SideEffectCoordinator(intents=world.intents)
    await _commit(coord, world, _router(None), idempotency_key="op-1")
    row = world.intents.only()
    row.max_attempts = 3
    for _ in range(5):
        await _recover(SideEffectCoordinator(intents=world.intents), _router(None))
    assert row.state == "failed_terminal"
    assert row.attempts == 3
    assert row.last_error and row.last_error.startswith("retry_exhausted")
    assert [o for _k, o in world.intents.settles] == ["retry", "retry", "retry"]


@pytest.mark.asyncio
async def test_retry_exhaustion_on_the_inline_leg_is_reported_terminal() -> None:
    world = _world()
    coord = SideEffectCoordinator(intents=world.intents)

    async def persist(**_: Any) -> uuid.UUID:
        spec = pg_store._graph_intent_var.get()
        assert spec is not None
        world.intents.record(replace(spec, max_attempts=1))
        return uuid.uuid4()

    world.persist.side_effect = persist
    report = await _commit(coord, world, _router(None), idempotency_key="op-1")
    assert report.graph_terminal and not report.graph_synced
    assert "graph_sync:retry_exhausted" in report.errors


# ── A held or out-of-order intent is never sent twice ───────────────────────


@pytest.mark.asyncio
async def test_an_intent_leased_elsewhere_is_deferred_not_sent() -> None:
    world = _world()
    coord = SideEffectCoordinator(intents=world.intents)
    await _commit(
        coord, world, _router(), idempotency_key="op-1", graph_sync=False, defer_graph_sync=True
    )
    await world.intents.claim(owner="other-worker", tenant_id=TENANT, effect_key=coord_key(world))
    router = _router(ACK)
    with patch("app.engines.packet_router.get_router", return_value=router):
        assert coord.schedule_graph_leg(coord_key(world), settings=SETTINGS)
        await coord.drain_graph_legs()
    router.notify_graph_sync.assert_not_awaited()
    assert world.intents.only().state == "dispatching"


def coord_key(world: _World) -> str:
    return world.intents.only().effect_key


@pytest.mark.asyncio
async def test_a_store_that_cannot_confirm_the_claim_sends_nothing() -> None:
    """Ownership unknown: another worker may hold it; recovery sends it instead."""
    world = _world()
    broken = MagicMock()
    broken.configured = MagicMock(return_value=True)
    broken.claim = AsyncMock(side_effect=RuntimeError("db down"))
    broken.get = AsyncMock(side_effect=RuntimeError("db down"))
    broken.settle = AsyncMock()
    coord = SideEffectCoordinator(intents=broken)
    router = _router(ACK)
    report = await _commit(coord, world, router, idempotency_key="op-1")
    router.notify_graph_sync.assert_not_awaited()
    assert report.graph_deferred and not report.graph_synced
    assert report.key in coord._graph_pending
    broken.settle.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_a_durable_store_the_leg_is_sent_unclaimed() -> None:
    world = _world()
    world.intents.configured_store = False
    coord = SideEffectCoordinator(intents=world.intents)
    router = _router(ACK)
    report = await _commit(coord, world, router, idempotency_key="op-1")
    assert report.graph_synced and not report.graph_durable
    assert router.notify_graph_sync.await_count == 1


@pytest.mark.asyncio
async def test_an_ack_the_intent_did_not_record_keeps_the_key_pending() -> None:
    """CEG acknowledged, but the lease was lost: the durable row decides, not this call."""
    world = _world()
    coord = SideEffectCoordinator(intents=world.intents)
    world.intents.settle = AsyncMock(return_value=None)  # type: ignore[method-assign]
    report = await _commit(coord, world, _router(ACK), idempotency_key="op-1")
    assert report.graph_synced and report.graph_unsettled
    assert report.key in coord._graph_pending
    assert report.key not in coord._completed


@pytest.mark.asyncio
async def test_an_unpinned_intent_is_refused_not_sent() -> None:
    world = _world()
    coord = SideEffectCoordinator(intents=world.intents)
    router = _router(ACK)
    router.graph_sync_pin = MagicMock(return_value=None)  # no contract resolved at commit
    report = await _commit(coord, world, router, idempotency_key="op-1")
    router.notify_graph_sync.assert_not_awaited()
    assert report.graph_terminal and "graph_sync:refused:unpinned" in report.errors
    assert world.intents.only().state == "failed_terminal"


@pytest.mark.asyncio
async def test_the_send_is_keyed_to_the_durable_operation() -> None:
    world = _world()
    coord = SideEffectCoordinator(intents=world.intents)
    router = _router(ACK)
    report = await _commit(coord, world, router, idempotency_key="op-1")
    assert router.notify_graph_sync.await_args.kwargs["operation_key"] == report.key


@pytest.mark.asyncio
async def test_converge_intents_are_held_while_converge_graph_sync_is_off() -> None:
    world = _world()
    await _commit(
        SideEffectCoordinator(intents=world.intents),
        world,
        _router(),
        idempotency_key="op-1",
        graph_sync=False,
        defer_graph_sync=True,
        graph_origin="converge",
    )
    off = SimpleNamespace(gate_url="https://gate.example", converge_graph_sync_enabled=False)
    router = _router(ACK)
    restarted = SideEffectCoordinator(intents=world.intents)
    with patch("app.engines.packet_router.get_router", return_value=router):
        assert await restarted.recover_graph_intents(off) == 0
    router.notify_graph_sync.assert_not_awaited()
    assert world.intents.only().state == "pending"
    assert await _recover(restarted, router) == 1  # flag back on
    assert world.intents.only().state == "acknowledged"


@pytest.mark.asyncio
async def test_recovery_records_the_linked_result_as_persisted() -> None:
    world = _world()
    await _commit(
        SideEffectCoordinator(intents=world.intents),
        world,
        _router(),
        idempotency_key="op-1",
        graph_sync=False,
        defer_graph_sync=True,
    )
    restarted = SideEffectCoordinator(intents=world.intents)
    await _recover(restarted, _router(ACK))
    key = world.intents.only().effect_key
    assert key in restarted._completed and key in restarted._persisted
    again = await _commit(
        restarted, world, _router(ACK), idempotency_key="op-1", require_persistence=True
    )
    assert again.skipped is True


@pytest.mark.asyncio
async def test_recovery_claims_one_intent_at_a_time() -> None:
    world = _world()
    for n in range(3):
        await _commit(
            SideEffectCoordinator(intents=world.intents),
            world,
            _router(),
            idempotency_key=f"op-{n}",
            entity_id=f"d-{n}",
            graph_sync=False,
            defer_graph_sync=True,
        )
    limits: list[int] = []
    claim_due = world.intents.claim_due

    async def spy(**kwargs: Any) -> list[Any]:
        limits.append(kwargs["limit"])
        return await claim_due(**kwargs)

    world.intents.claim_due = spy  # type: ignore[method-assign]
    assert await _recover(SideEffectCoordinator(intents=world.intents), _router(ACK, ACK, ACK)) == 3
    assert set(limits) == {1}


@pytest.mark.asyncio
async def test_a_poll_from_an_earlier_lifespan_is_replaced() -> None:
    coord = SideEffectCoordinator(intents=FakeIntents())
    assert coord.start_graph_recovery(SETTINGS, interval_seconds=60)
    first = coord._recovery_task
    assert first is not None
    await coord.stop_graph_recovery()
    assert coord.start_graph_recovery(SETTINGS, interval_seconds=60)
    assert coord._recovery_task is not first
    await coord.stop_graph_recovery()


@pytest.mark.asyncio
async def test_no_graph_leg_asked_records_no_intent() -> None:
    world = _world()
    coord = SideEffectCoordinator(intents=world.intents)
    report = await _commit(coord, world, _router(), idempotency_key="op-1", graph_sync=False)
    assert report.graph_skipped and not report.graph_durable
    assert world.intents.rows == {}


# ── P15/P21: an idle restarted node recovers without traffic ────────────────


@pytest.mark.asyncio
async def test_the_recovery_poll_replays_due_intents_on_an_idle_node() -> None:
    world = _world()
    await _commit(
        SideEffectCoordinator(intents=world.intents),
        world,
        _router(),
        idempotency_key="op-1",
        graph_sync=False,
        defer_graph_sync=True,
    )
    restarted = SideEffectCoordinator(intents=world.intents)
    router = _router(ACK)
    with patch("app.engines.packet_router.get_router", return_value=router):
        assert restarted.start_graph_recovery(SETTINGS, interval_seconds=0.01) is True
        assert restarted.start_graph_recovery(SETTINGS, interval_seconds=0.01) is False
        for _ in range(100):
            if world.intents.only().state == "acknowledged":
                break
            await asyncio.sleep(0.01)
        await restarted.stop_graph_recovery()
    assert world.intents.only().state == "acknowledged"
    assert router.notify_graph_sync.await_count == 1


@pytest.mark.asyncio
async def test_a_failing_recovery_pass_is_retried_on_the_next_tick() -> None:
    intents = MagicMock()
    intents.claim_due = AsyncMock(side_effect=[RuntimeError("store not ready"), []])
    coord = SideEffectCoordinator(intents=intents)
    assert coord.start_graph_recovery(SETTINGS, interval_seconds=0.01)
    for _ in range(100):
        if intents.claim_due.await_count >= 2:
            break
        await asyncio.sleep(0.01)
    await coord.stop_graph_recovery()
    assert intents.claim_due.await_count >= 2


def test_startup_registration_outside_a_loop_starts_no_poll() -> None:
    from app.services.chassis_handlers import start_graph_intent_recovery

    assert start_graph_intent_recovery() is False


@pytest.mark.asyncio
async def test_startup_registration_starts_the_singletons_poll_once() -> None:
    from app.services.chassis_handlers import start_graph_intent_recovery
    from app.services.side_effect_coordinator import get_side_effect_coordinator

    coord = get_side_effect_coordinator()
    try:
        assert start_graph_intent_recovery() is True
        assert start_graph_intent_recovery() is False
    finally:
        await coord.stop_graph_recovery()


# ── Review round 3 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_mismatched_replay_is_a_failed_persistence_not_completed() -> None:
    """The stored result does not hold this call's values: never answered `completed`."""
    from app.services.side_effect_coordinator import PersistenceRequiredError

    world = _world()
    world.persist.side_effect = pg_store.GraphIntentReplayMismatchError(TENANT, "op-1")
    coord = SideEffectCoordinator(intents=world.intents)
    with pytest.raises(PersistenceRequiredError):
        await _commit(
            coord,
            world,
            _router(ACK),
            idempotency_key="op-1",
            require_persistence=True,
            graph_sync=False,
            defer_graph_sync=True,
        )
    key = next(iter(coord._in_flight), None)
    assert key is None
    assert not coord._completed and not coord._persisted
    world.emitter.emit_enrichment_completed.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_finishing_first_leaves_no_pending_beside_completed() -> None:
    world = _world()
    coord = SideEffectCoordinator(intents=world.intents)
    router = _router(ACK)

    async def emit_while_recovery_runs(**_: Any) -> None:
        # Recovery resolves the intent while the foreground emit is awaited.
        await _recover(coord, router)

    world.emitter.emit_enrichment_completed = AsyncMock(side_effect=emit_while_recovery_runs)
    report = await _commit(
        coord, world, _router(), idempotency_key="op-1", graph_sync=False, defer_graph_sync=True
    )
    assert report.key in coord._completed
    assert report.key not in coord._graph_pending
    assert coord.schedule_graph_leg(report.key, settings=SETTINGS) is False
    assert world.intents.only().state == "acknowledged"


def test_a_completed_key_never_starts_a_graph_leg() -> None:
    coord = SideEffectCoordinator(intents=FakeIntents())
    coord._graph_pending["k"] = {
        "tenant": TENANT,
        "entity_id": "d-1",
        "fields": {},
        "domain": TENANT,
    }
    coord._completed.add("k")
    assert coord.schedule_graph_leg("k", settings=SETTINGS) is False


def test_a_poll_bound_to_a_closed_loop_is_replaced_without_cancelling() -> None:
    coord = SideEffectCoordinator(intents=FakeIntents())
    old_loop = asyncio.new_event_loop()

    async def start() -> None:
        assert coord.start_graph_recovery(SETTINGS, interval_seconds=60)

    old_loop.run_until_complete(start())
    stale = coord._recovery_task
    old_loop.close()  # the lifespan's loop is gone; its task never finished

    async def restart() -> None:
        assert coord.start_graph_recovery(SETTINGS, interval_seconds=60)
        assert coord._recovery_task is not stale
        await coord.stop_graph_recovery()

    asyncio.run(restart())


@pytest.mark.asyncio
async def test_a_claim_whose_lease_ran_out_before_the_send_sends_nothing() -> None:
    """A slow claim can return an already-expired lease; another worker may own it."""
    from datetime import UTC, datetime, timedelta

    world = _world()
    await _commit(
        SideEffectCoordinator(intents=world.intents),
        world,
        _router(),
        idempotency_key="op-1",
        graph_sync=False,
        defer_graph_sync=True,
    )
    (claimed,) = await world.intents.claim_due(owner="w1", limit=1)
    expired = replace(claimed, lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    router = _router(ACK)
    report = await _send_claimed(SideEffectCoordinator(intents=world.intents), router, expired)
    router.notify_graph_sync.assert_not_awaited()
    assert report.graph_deferred and "graph_sync:lease_expired_before_send" in report.errors
    world.intents.settles.clear()


async def _send_claimed(coord: SideEffectCoordinator, router: MagicMock, record: Any) -> Any:
    from app.services.side_effect_coordinator import SideEffectReport

    report = SideEffectReport(key=record.effect_key, graph_retry=True)
    with patch("app.engines.packet_router.get_router", return_value=router):
        await coord._graph_leg(
            key=record.effect_key,
            tenant=record.tenant_id,
            entity_id=record.entity_id,
            fields=record.fields,
            domain=record.domain or record.tenant_id,
            node_label=record.node_label,
            contract_digest=record.contract_digest,
            settings=SETTINGS,
            report=report,
            claimed=record,
        )
    return report
