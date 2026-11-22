"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: enrichment
layer: [services]
tags: [side-effects, idempotency, gate-only]
owner: engine-team
status: active
--- /L9_META ---

Single side-effect coordinator for EIE post-enrichment work (TASK-021).

Guarantees at most one persistence and one enrichment-completed event per
semantic request key, and records the graph sync as done only once CEG
acknowledges it — an unacknowledged graph leg is the one effect a retry of
the same key may attempt again (RV-DOMAIN-RD-07). Score invalidation is not
an effect: Gate does not own ``score-invalidate``, so this coordinator does
not dispatch it and never reports ``score_invalidated``. All egress is
Gate-only via PacketRouter / event emitter.

A latency-bounded caller may DEFER the graph leg (`defer_graph_sync=True`):
the key is left pending for its graph leg exactly as an unacknowledged sync
leaves it, and the caller later starts that leg detached from its own request
with `schedule_graph_leg`. At most one graph leg per key is in flight at a
time, awaited or detached, so a retry of the same operation never sends a
second sync while one is outstanding.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import threading
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.services.request_deadline import detached_context

logger = structlog.get_logger("side_effect_coordinator")


def semantic_side_effect_key(
    *,
    tenant: str,
    entity_id: str,
    action: str = "enrich",
    packet_id: str | None = None,
    idempotency_key: str | None = None,
    result_digest: str | None = None,
) -> str:
    """Key one operation's side effects (WU-01 / F-001).

    Preference:

    1. the caller's idempotency key — the caller's own declaration of the
       logical operation. A caller retrying after a timeout sends a NEW packet
       with the SAME key; keying on the packet first re-ran every effect;
    2. the transport packet id (``packet.header.packet_id``, immutable and
       signed — never a payload field, D-7), scoped by tenant;
    3. the result itself. Two runs for one entity that produced different
       results are different operations and persist twice; a byte-identical
       result is the same effect and persists once. The legacy
       tenant|entity|action hash (no digest) collapsed every run of an entity
       into one key, so the second run was skipped and never persisted.
    """
    if idempotency_key:
        return f"idem:{tenant}:{idempotency_key}:{action}"
    if packet_id:
        return f"pkt:{packet_id}:{tenant}:{action}"
    material = f"{tenant}|{entity_id}|{action}"
    if result_digest:
        digest = hashlib.sha256(f"{material}|{result_digest}".encode()).hexdigest()[:24]
        return f"res:{digest}"
    digest = hashlib.sha256(material.encode()).hexdigest()[:24]
    return f"hash:{digest}"


#: Upper bound on remembered keys per marker. Every SDK packet and every distinct
#: result adds a key, so the markers must not grow for the life of the process.
#: The oldest key is forgotten first; a replay older than this many operations
#: is no longer recognised as a duplicate (the store's own idempotency key
#: still dedupes the row).
MAX_TRACKED_KEYS = 100_000


class _BoundedKeys:
    """Insertion-ordered key set that forgets its oldest entries past a bound."""

    def __init__(self, maxsize: int = MAX_TRACKED_KEYS) -> None:
        self._keys: OrderedDict[str, None] = OrderedDict()
        self._maxsize = maxsize

    def __contains__(self, key: object) -> bool:
        return key in self._keys

    def __len__(self) -> int:
        return len(self._keys)

    def add(self, key: str) -> None:
        self._keys[key] = None
        self._keys.move_to_end(key)
        while len(self._keys) > self._maxsize:
            self._keys.popitem(last=False)

    def discard(self, key: str) -> None:
        self._keys.pop(key, None)

    def clear(self) -> None:
        self._keys.clear()


class _BoundedPending(OrderedDict[str, dict[str, Any]]):
    """Graph-pending entries, bounded like the key markers."""

    def __init__(self, maxsize: int = MAX_TRACKED_KEYS) -> None:
        super().__init__()
        self._maxsize = maxsize

    def __setitem__(self, key: str, value: dict[str, Any]) -> None:
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > self._maxsize:
            self.popitem(last=False)


def result_digest(response_dict: dict[str, Any]) -> str:
    """Stable digest of what a result would persist and emit."""
    material = json.dumps(
        {
            "fields": response_dict.get("fields") or {},
            "confidence": response_dict.get("confidence"),
            "state": response_dict.get("state"),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:24]


class PersistenceRequiredError(RuntimeError):
    """Durability was a precondition of the answer, and it did not hold.

    Raised only when a caller passed ``require_persistence=True``. It is not an
    enrichment failure — the result was computed — it is a refusal to report
    that result as durable when no store accepted it.
    """


@dataclass
class SideEffectReport:
    key: str
    persisted: bool = False
    graph_synced: bool = False
    graph_skipped: bool = False
    # Kept so the report shape stays stable. Always False: Gate does not own
    # score-invalidate, so no invalidation is dispatched or acknowledged.
    score_invalidated: bool = False
    event_emitted: bool = False
    skipped: bool = False
    # True when this call re-attempted ONLY the Gate->GRAPH leg of a key whose
    # other effects already completed on an earlier attempt (RV-DOMAIN-RD-07).
    graph_retry: bool = False
    # True when the graph leg was not awaited by this call: the caller deferred
    # it, or a detached leg for the key was already in flight. The key is left
    # pending for its graph leg; `graph_synced` stays False.
    graph_deferred: bool = False
    # A2: True when the graph obligation of this key is held durably -- recorded
    # in the result's own transaction (graph_sync_intents) -- so a restart
    # cannot lose it. False when no store accepted it (persistence failed and
    # was not required, or the graph leg was not asked for).
    graph_durable: bool = False
    # A2: True when the graph leg ended terminally without a sync: CEG or the
    # contract refused the pinned input, or its retries were spent. Never
    # reported as synced; the reason is in `errors` (`graph_sync:refused:*`).
    graph_terminal: bool = False
    # A2: True when CEG's answer (synced or terminal) was observed but the
    # durable intent did not record it (lease lost, store error). The key then
    # stays pending: the durable row, not this call, decides completion.
    graph_unsettled: bool = False
    errors: list[str] = field(default_factory=list)


#: How long one graph-leg attempt holds its durable claim. Longer than the
#: worst-case awaited `notify_graph_sync` (two 10 s attempts plus 1 s backoff),
#: so a live attempt is never re-claimed under it; a holder that died releases
#: the intent when the lease expires.
GRAPH_LEASE_SECONDS = 60.0
#: Backoff between durable graph-leg attempts (doubles per attempt, capped).
GRAPH_RETRY_BASE_SECONDS = 2.0
GRAPH_RETRY_CAP_SECONDS = 300.0


class PgGraphIntents:
    """The coordinator's view of the durable graph-intent store (`pg_store`).

    Imported lazily: the coordinator module must stay importable where no DB
    runtime is installed.
    """

    async def claim(self, *, owner: str, tenant_id: str, effect_key: str) -> list[Any]:
        from app.services import pg_store

        return await pg_store.claim_graph_intents(
            owner=owner,
            lease_seconds=GRAPH_LEASE_SECONDS,
            limit=1,
            tenant_id=tenant_id,
            effect_key=effect_key,
        )

    async def claim_due(
        self, *, owner: str, limit: int, held_origins: frozenset[str] = frozenset()
    ) -> list[Any]:
        from app.services import pg_store

        return await pg_store.claim_graph_intents(
            owner=owner, lease_seconds=GRAPH_LEASE_SECONDS, limit=limit, held_origins=held_origins
        )

    def configured(self) -> bool:
        """Whether a durable store backs this process (its engine is initialised)."""
        try:
            from app.services import pg_store
        except ImportError:  # no DB runtime installed: nothing durable exists
            return False
        return pg_store._engine is not None

    async def get(self, tenant_id: str, effect_key: str) -> Any | None:
        from app.services import pg_store

        return await pg_store.get_graph_intent(tenant_id, effect_key)

    async def settle(
        self,
        intent_id: Any,
        *,
        owner: str,
        outcome: str,
        error: str | None,
        ack: Mapping[str, Any] | None,
    ) -> str | None:
        from app.services import pg_store

        return await pg_store.settle_graph_intent(
            intent_id,
            owner=owner,
            outcome=outcome,
            error=error,
            ack=ack,
            retry_base_seconds=GRAPH_RETRY_BASE_SECONDS,
            retry_cap_seconds=GRAPH_RETRY_CAP_SECONDS,
        )


class SideEffectCoordinator:
    """Process-local once-per-key coordinator for enrich follow-up effects.

    Two markers, one key space:

    * ``_completed`` — every effect the call asked for either ran or was
      explicitly excluded. A later call for the key is a duplicate and skips.
    * ``_graph_pending`` — persistence and the completion event ran, but CEG
      did not acknowledge the graph sync. A later call for
      the key re-attempts the graph leg ONLY, so a legitimate retry can finish
      the sync without duplicating effects that already happened. The entry
      holds the ``entity_id``, ``fields`` and ``domain`` that WERE persisted and
      emitted, and the retry sends exactly those: the graph must end up agreeing with the
      store and the event, not with whatever the retrying call happens to carry
      (a later enrichment of the same entity can key identically under the
      tenant/entity fallback, or re-run with a different LLM answer).

    A key is never in both. Both are process-local, like the guard always was.
    """

    def __init__(self, max_keys: int = MAX_TRACKED_KEYS, *, intents: Any | None = None) -> None:
        # A2: the durable graph-intent store and this process's claim identity.
        self._intents = intents if intents is not None else PgGraphIntents()
        self._owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"[:128]
        self._recovery_task: asyncio.Task[None] | None = None
        self._completed = _BoundedKeys(max_keys)
        # Keys whose result a store accepted. A key can be completed without
        # it when persistence was not required and failed (F-001).
        self._persisted = _BoundedKeys(max_keys)
        self._graph_pending: dict[str, dict[str, Any]] = _BoundedPending(max_keys)
        # Reserved before the first await (F-024): a concurrent call for the
        # same key waits for the holder and then re-evaluates.
        self._in_flight: dict[str, asyncio.Event] = {}
        # Keys whose detached graph leg is scheduled or running. Added when the
        # task is created, not when it starts, so no call can slip a second
        # sync for the key in between.
        self._graph_leg_in_flight: set[str] = set()
        self._graph_leg_tasks: set[asyncio.Task[SideEffectReport]] = set()

    def reset_for_tests(self) -> None:
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            self._recovery_task = None
        for task in self._graph_leg_tasks:
            task.cancel()
        self._graph_leg_tasks.clear()
        self._graph_leg_in_flight.clear()
        self._completed.clear()
        self._persisted.clear()
        self._graph_pending.clear()
        self._in_flight.clear()

    def schedule_graph_leg(self, key: str, *, settings: Any) -> bool:
        """Start the pending graph leg of `key` detached from the caller.

        The leg runs as its own task, outside any request deadline
        (`detached_context`), and syncs exactly what the pending entry pinned:
        the entity, admitted fields, domain and node label that were persisted.
        Its outcome is the graph leg's alone: acknowledged by CEG, the key is
        completed; anything else leaves it pending for a later graph leg, as an
        unacknowledged awaited sync would. Nothing it does reaches the caller.

        Returns False, starting nothing, when the key has no pending graph leg
        (already synced, or never deferred) or a leg for it is already in
        flight — one sync per key at a time.
        """
        if (
            key not in self._graph_pending
            or key in self._graph_leg_in_flight
            or key in self._completed
        ):
            return False
        self._graph_leg_in_flight.add(key)
        task = asyncio.create_task(self._run_graph_leg(key, settings), context=detached_context())
        self._graph_leg_tasks.add(task)
        task.add_done_callback(self._graph_leg_tasks.discard)
        logger.info("side_effects_graph_leg_scheduled", key=key)
        return True

    async def drain_graph_legs(self) -> None:
        """Wait for every detached graph leg in flight (tests, graceful shutdown)."""
        while self._graph_leg_tasks:
            await asyncio.gather(*list(self._graph_leg_tasks), return_exceptions=True)

    async def _run_graph_leg(self, key: str, settings: Any) -> SideEffectReport:
        """The detached graph leg; see `schedule_graph_leg`."""
        report = SideEffectReport(key=key, graph_retry=True)
        try:
            pending = self._graph_pending.get(key)
            if pending is None:
                return report
            await self._graph_leg(
                key=key,
                tenant=pending["tenant"],
                entity_id=pending["entity_id"],
                fields=pending["fields"],
                domain=pending["domain"],
                node_label=pending.get("node_label"),
                contract_digest=pending.get("contract_digest"),
                settings=settings,
                report=report,
            )
            if _leg_resolved(report):
                self._graph_pending.pop(key, None)
                self._completed.add(key)
                logger.info(
                    "side_effects_graph_leg_resolved",
                    key=key,
                    entity_id=pending["entity_id"],
                    synced=report.graph_synced,
                    terminal=report.graph_terminal,
                )
            else:
                logger.warning(
                    "side_effects_graph_leg_unacknowledged",
                    key=key,
                    entity_id=pending["entity_id"],
                    deferred=report.graph_deferred,
                    errors=report.errors,
                )
        except Exception as exc:  # noqa: BLE001 — the leg's failure stays the leg's
            report.errors.append(f"graph_sync:{exc}")
            logger.warning("side_effects_graph_leg_failed", key=key, error=str(exc))
        finally:
            self._graph_leg_in_flight.discard(key)
        return report

    def _store_configured(self) -> bool:
        configured = getattr(self._intents, "configured", None)
        return bool(configured()) if callable(configured) else True

    async def _graph_leg(
        self,
        *,
        key: str,
        tenant: str,
        entity_id: str,
        fields: Mapping[str, Any],
        domain: str,
        node_label: str | None,
        contract_digest: str | None,
        settings: Any,
        report: SideEffectReport,
        claimed: Any | None = None,
    ) -> None:
        """One attempt of the graph leg of `key`, under its durable claim (A2).

        `_lease_intent` decides whether this call may send: with its durable
        intent leased, the pinned input is sent and the attempt is settled on
        that lease; with no durable layer, or no intent for the key, the leg is
        sent unclaimed (a replay is a MERGE under the same key); anything else
        -- an intent held elsewhere, behind an older version, already resolved,
        or a store that cannot confirm ownership -- sends nothing.

        `report.graph_synced` / `graph_terminal` record what CEG answered;
        `graph_unsettled` records that the durable intent did not take that
        answer, so callers keep the key pending rather than completing it.
        """
        proceed, record = await self._lease_intent(key, tenant, claimed, report)
        if not proceed:
            return
        if record is not None and _lease_expired(record):
            # The claim took longer than its lease: another worker may hold the
            # row now. Nothing is sent on a lease this call no longer owns.
            report.graph_deferred = True
            report.errors.append("graph_sync:lease_expired_before_send")
            logger.warning("side_effects_graph_lease_expired", key=key)
            return
        if record is not None:
            entity_id = record.entity_id
            fields = record.fields
            domain = record.domain or domain
            node_label = record.node_label
            contract_digest = record.contract_digest
            tenant = record.tenant_id
            if contract_digest is None:
                # Never pinned at commit: no contract is known to have admitted
                # these fields, so they are not sent to whatever resolves now.
                report.graph_terminal = True
                report.errors.append("graph_sync:refused:unpinned")
                await self._settle_intent(record, "terminal", "refused:unpinned", None, report)
                return
        ack, outcome, raised = await self._send_graph_sync(
            settings,
            key=key,
            tenant=tenant,
            entity_id=entity_id,
            fields=fields,
            domain=domain,
            node_label=node_label,
            contract_digest=contract_digest,
            report=report,
        )
        disposition, error = self._classify_graph_outcome(
            key, entity_id, ack, outcome, raised, report
        )
        if record is not None:
            await self._settle_intent(
                record, disposition, error, ack if report.graph_synced else None, report
            )

    async def _lease_intent(
        self, key: str, tenant: str, claimed: Any | None, report: SideEffectReport
    ) -> tuple[bool, Any | None]:
        """(may send, leased intent or None); see `_graph_leg`."""
        if claimed is not None:
            return True, claimed
        if not self._store_configured():
            return True, None
        try:
            leased = await self._intents.claim(owner=self._owner, tenant_id=tenant, effect_key=key)
            if leased:
                return True, leased[0]
            existing = await self._intents.get(tenant, key)
        except Exception as exc:  # noqa: BLE001 — ownership unknown: recovery sends it
            report.graph_deferred = True
            report.errors.append(f"graph_sync:claim_unavailable:{exc}")
            logger.warning("side_effects_graph_claim_unavailable", key=key, error=str(exc))
            return False, None
        if existing is None:
            return True, None
        self._report_unclaimable(existing, report)
        return False, None

    async def _send_graph_sync(
        self,
        settings: Any,
        *,
        key: str,
        tenant: str,
        entity_id: str,
        fields: Mapping[str, Any],
        domain: str,
        node_label: str | None,
        contract_digest: str | None,
        report: SideEffectReport,
    ) -> tuple[Any, Any | None, bool]:
        """Send one sync; returns (ack, classified outcome or None, raised)."""
        from app.engines.packet_router import get_router

        outcomes: list[Any] = []
        try:
            ack = await get_router(settings).notify_graph_sync(
                tenant_id=tenant,
                entity_id=entity_id,
                fields=dict(fields),
                domain=domain,
                node_label=node_label,
                expected_contract_digest=contract_digest,
                on_outcome=outcomes.append,
                operation_key=key,
            )
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"graph_sync:{exc}")
            logger.warning("side_effect_graph_sync_failed", entity_id=entity_id, error=str(exc))
            return None, None, True
        return ack, (outcomes[-1] if outcomes else None), False

    @staticmethod
    def _classify_graph_outcome(
        key: str,
        entity_id: str,
        ack: Any,
        outcome: Any | None,
        raised: bool,
        report: SideEffectReport,
    ) -> tuple[str, str | None]:
        """(disposition, error) of one sent attempt; sets synced / terminal on `report`."""
        from app.engines.packet_router import GraphSyncOutcomeKind, graph_sync_acknowledged

        if not raised and graph_sync_acknowledged(ack):
            report.graph_synced = True
            return "acknowledged", None
        if outcome is not None and outcome.kind is GraphSyncOutcomeKind.REFUSED:
            report.graph_terminal = True
            report.errors.append(f"graph_sync:refused:{outcome.reason}")
            logger.warning(
                "side_effect_graph_sync_refused",
                key=key,
                entity_id=entity_id,
                reason=outcome.reason,
            )
            return "terminal", f"refused:{outcome.reason}"
        if not raised:
            report.errors.append(f"graph_sync:unacknowledged:{_ack_summary(ack)}")
            logger.warning(
                "side_effect_graph_sync_unacknowledged",
                entity_id=entity_id,
                key=key,
                ack=_ack_summary(ack),
            )
        return "retry", f"unacknowledged:{outcome.reason if outcome else _ack_summary(ack)}"

    async def _settle_intent(
        self,
        record: Any,
        disposition: str,
        error: str | None,
        ack: Any,
        report: SideEffectReport,
    ) -> None:
        """Settle the leased intent on its own fencing token; see `_graph_leg`."""
        try:
            state = await self._intents.settle(
                record.id,
                owner=record.lease_owner,
                outcome=disposition,
                error=error,
                ack=ack,
            )
        except Exception as exc:  # noqa: BLE001 — the row stays claimable; a replay is safe
            report.graph_unsettled = True
            logger.warning(
                "side_effects_graph_settle_failed", key=record.effect_key, error=str(exc)
            )
            return
        if state is None:
            report.graph_unsettled = True
            logger.warning("side_effects_graph_settle_lease_lost", key=record.effect_key)
            return
        if disposition == "retry" and state == "failed_terminal":
            report.graph_terminal = True
            report.errors.append("graph_sync:retry_exhausted")
            logger.error("side_effects_graph_retry_exhausted", key=record.effect_key)

    @staticmethod
    def _report_unclaimable(existing: Any, report: SideEffectReport) -> None:
        """Report an intent this call could not claim, as it stands."""
        state = getattr(existing, "state", None)
        if state == "acknowledged":
            report.graph_synced = True
        elif state in {"failed_terminal", "superseded"}:
            report.graph_terminal = True
            report.errors.append(f"graph_sync:{state}:{getattr(existing, 'last_error', None)}")
        else:
            # Held by another worker's lease, waiting for its backoff, or behind
            # an older version of the same subject: recovery owns it.
            report.graph_deferred = True

    async def recover_graph_intents(self, settings: Any, *, limit: int = 50) -> int:
        """Send due durable graph intents, one claim at a time; returns how many.

        This is what makes a pending intent survive a restart: the intents a
        dead process left `pending` (or leased and never settled) are claimed
        and replayed with their pinned input. Each intent is claimed only when
        it is about to be sent, so a lease never expires while it waits behind
        others, and several workers may run this at once -- each intent is
        leased to exactly one. While converge graph sync is switched off,
        converge-origin intents are held (neither claimed nor resolved).
        """
        held = frozenset() if _converge_graph_sync_enabled(settings) else frozenset({"converge"})
        attempted = 0
        while attempted < limit:
            claimed = await self._intents.claim_due(owner=self._owner, limit=1, held_origins=held)
            if not claimed:
                break
            record = claimed[0]
            attempted += 1
            report = SideEffectReport(key=record.effect_key, graph_retry=True)
            try:
                await self._graph_leg(
                    key=record.effect_key,
                    tenant=record.tenant_id,
                    entity_id=record.entity_id,
                    fields=record.fields,
                    domain=record.domain or record.tenant_id,
                    node_label=record.node_label,
                    contract_digest=record.contract_digest,
                    settings=settings,
                    report=report,
                    claimed=record,
                )
            except Exception as exc:  # noqa: BLE001 — one intent's failure stays its own
                logger.warning(
                    "side_effects_graph_recovery_failed", key=record.effect_key, error=str(exc)
                )
                continue
            if _leg_resolved(report):
                self._graph_pending.pop(record.effect_key, None)
                self._completed.add(record.effect_key)
                if getattr(record, "enrichment_result_id", None) is not None:
                    # The intent was written with its result: the result is durable.
                    self._persisted.add(record.effect_key)
        if attempted:
            logger.info("side_effects_graph_recovered", attempted=attempted)
        return attempted

    def start_graph_recovery(self, settings: Any, *, interval_seconds: float) -> bool:
        """Run `recover_graph_intents` every `interval_seconds` in this process.

        Returns False, starting nothing, for a non-positive interval or when the
        loop already runs on the current event loop. A task left from an
        earlier lifespan (finished, or bound to a closed loop) is replaced.
        """
        if interval_seconds <= 0:
            return False
        current = self._recovery_task
        if current is not None and not current.done():
            previous_loop = current.get_loop()
            if previous_loop is asyncio.get_running_loop():
                return False
            if not previous_loop.is_closed():
                # A closed loop cannot run the cancellation; the task is dead
                # with it, so it is only dropped.
                current.cancel()
        self._recovery_task = asyncio.create_task(
            self._recovery_loop(settings, interval_seconds), context=detached_context()
        )
        return True

    async def stop_graph_recovery(self) -> None:
        task, self._recovery_task = self._recovery_task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _recovery_loop(self, settings: Any, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await self.recover_graph_intents(settings)
            except Exception as exc:  # noqa: BLE001 — a failed pass is retried next tick
                logger.warning("side_effects_graph_recovery_pass_failed", error=str(exc))

    async def commit_after_enrich(
        self,
        *,
        tenant: str,
        entity_id: str,
        object_type: str,
        domain: str,
        response_dict: dict[str, Any],
        settings: Any,
        packet_id: str | None = None,
        idempotency_key: str | None = None,
        emit_event: bool = True,
        graph_sync: bool = True,
        require_persistence: bool = False,
        node_label: str | None = None,
        defer_graph_sync: bool = False,
        graph_fields: Mapping[str, Any] | None = None,
        graph_origin: str = "enrich",
    ) -> SideEffectReport:
        """Commit post-enrich side effects once per semantic key.

        `graph_sync=False` excludes the Gate->GRAPH round trip, and ONLY that.
        A caller that is itself answering a latency-bounded request uses it:
        `notify_graph_sync` awaits up to two Gate attempts at 10 s each with a
        1 s backoff between (packet_router._MAX_ATTEMPTS / _ROUTE_TIMEOUT) --
        21 s worst case, which a 30 s synchronous caller budget cannot absorb
        on top of the enrichment itself.

        The flag is named `graph_sync` rather than `side_effects` because it
        excludes only the awaited Gate->GRAPH hop:

        * `notify_graph_sync` is **awaited**, and goes to GRAPH (CEG's `sync`).
          Its latency lands on the caller. That is what the flag excludes.
        * Score invalidation is not dispatched. Gate does not own
          `score-invalidate` (non-canonical; no node is registered), so there
          is no acknowledgment to record. `score_invalidated` stays False.
          This is not a skipped success.

        Anything that becomes an awaited Gate hop must move inside the guard,
        or take a flag of its own.

        `require_persistence=True` makes durability a precondition of the answer
        rather than a side effect of it: the persist failure is re-raised, and
        the semantic key is NOT recorded, so the same logical operation can be
        retried. Without it a caller that reports `completed` on the strength of
        a computed result would acknowledge work no store holds, and the
        once-per-key guard would then suppress every retry of it.

        `node_label` names the entity's ontology node (GRAPH-IDENT-02). The
        router selects the domain's CEG sync endpoint by it when the spec
        declares several; a domain with several endpoints and no label is
        skipped as ambiguous rather than sent to another entity type's
        endpoint. A graph-leg retry re-sends the label of the first attempt,
        like the fields and domain it pins.

        `defer_graph_sync=True` is the flag of its own that an awaited Gate hop
        needs to leave a bounded caller's path without being dropped: the graph
        leg is not awaited here, the key is left pending for it with this call's
        entity, fields, domain and label pinned, and `report.graph_deferred` is
        set. The caller starts the leg with `schedule_graph_leg(report.key)`
        once its own answer is decided, or never, leaving the key pending for a
        later call. A key whose detached leg is already in flight is never sent
        a second sync by any call.

        `graph_fields` is what the graph leg syncs, when it differs from the
        response's `fields` (a caller syncing only its admitted fields); the
        persisted record and the event still carry the response unchanged.

        `graph_origin` names the workflow the durable intent belongs to
        (`enrich`, `converge`); recovery holds a workflow's intents while that
        workflow's graph sync is switched off.
        """
        key = semantic_side_effect_key(
            tenant=tenant,
            entity_id=entity_id,
            action="enrich",
            packet_id=packet_id,
            idempotency_key=idempotency_key,
            result_digest=result_digest(response_dict),
        )
        # Reservation (F-024). Everything from here to `_in_flight[key] = …`
        # runs without an await, so on one event loop the check and the
        # reservation are atomic: of N concurrent calls for a key, one holds it
        # and the rest wait for it, then see its outcome.
        while key in self._in_flight:
            await self._in_flight[key].wait()
        if key in self._completed:
            if require_persistence and key not in self._persisted:
                # The earlier attempt completed without a durable result (its
                # persistence was not required and failed). Skipping now would
                # answer `completed` for a result no store holds.
                raise PersistenceRequiredError(
                    f"operation {key} completed earlier without a durable result"
                )
            report = SideEffectReport(key=key, skipped=True)
            logger.info("side_effects_skipped_duplicate", key=key, entity_id=entity_id)
            return report

        reservation = asyncio.Event()
        self._in_flight[key] = reservation
        try:
            return await self._commit_reserved(
                key=key,
                tenant=tenant,
                entity_id=entity_id,
                object_type=object_type,
                domain=domain,
                response_dict=response_dict,
                settings=settings,
                idempotency_key=idempotency_key,
                emit_event=emit_event,
                graph_sync=graph_sync,
                require_persistence=require_persistence,
                node_label=node_label,
                defer_graph_sync=defer_graph_sync,
                graph_fields=graph_fields,
                graph_origin=graph_origin,
            )
        finally:
            del self._in_flight[key]
            reservation.set()

    async def _commit_reserved(
        self,
        *,
        key: str,
        tenant: str,
        entity_id: str,
        object_type: str,
        domain: str,
        response_dict: dict[str, Any],
        settings: Any,
        idempotency_key: str | None,
        emit_event: bool,
        graph_sync: bool,
        require_persistence: bool,
        node_label: str | None,
        defer_graph_sync: bool = False,
        graph_fields: Mapping[str, Any] | None = None,
        graph_origin: str = "enrich",
    ) -> SideEffectReport:
        """Run the effects for a key this call holds the reservation for."""
        report = SideEffectReport(key=key)

        if require_persistence and key in self._graph_pending and key not in self._persisted:
            # A graph-leg retry skips persistence. When the first attempt did not
            # persist, that would answer `completed` for a result no store holds.
            raise PersistenceRequiredError(
                f"operation {key} is pending its graph leg without a durable result"
            )

        # A key left pending by an unacknowledged graph sync re-runs the graph
        # leg only: persistence, score invalidation and the completion event
        # already happened for it and must not happen twice.
        pending = self._graph_pending.get(key)
        graph_retry = pending is not None
        report.graph_retry = graph_retry
        # What the graph leg syncs: on a retry, the entity, fields and domain
        # that were persisted and emitted on the first attempt, never this call's.
        graph_tenant = tenant
        graph_entity_id = entity_id
        synced_fields: Mapping[str, Any] = (
            graph_fields if graph_fields is not None else response_dict.get("fields", {})
        )
        graph_domain = domain
        graph_node_label = node_label
        graph_contract_digest: str | None = None
        if pending is not None:
            graph_tenant = pending.get("tenant", tenant)
            graph_entity_id = pending["entity_id"]
            synced_fields = pending["fields"]
            graph_domain = pending["domain"]
            graph_node_label = pending.get("node_label")
            graph_contract_digest = pending.get("contract_digest")
            logger.info("side_effects_graph_retry", key=key, entity_id=entity_id)

        # 1) Persist once
        if not graph_retry:
            graph_leg_asked = graph_sync or defer_graph_sync
            intent = None
            try:
                from app.models.schemas import EnrichResponse
                from app.services import pg_store
                from app.services.result_store import ResultStore

                if graph_leg_asked:
                    # A2: the graph obligation is recorded in the result's own
                    # transaction, pinned to exactly what the leg sends, so a
                    # result answered `completed` cannot lose it to a restart.
                    pin = self._graph_pin(settings, tenant, node_label)
                    graph_contract_digest = pin.contract_digest if pin is not None else None
                    intent = pg_store.GraphIntentSpec(
                        tenant_id=tenant,
                        effect_key=key,
                        entity_id=entity_id,
                        fields=dict(synced_fields),
                        domain=domain,
                        node_label=node_label,
                        endpoint=pin.entity_type if pin is not None else None,
                        contract_digest=graph_contract_digest,
                        origin=graph_origin,
                    )
                store = ResultStore(tenant_id=tenant)
                resp_obj = EnrichResponse.model_validate(response_dict)
                with pg_store.graph_intent_scope(intent):
                    await store.persist_enrich_response(
                        response=resp_obj,
                        entity_id=entity_id,
                        object_type=object_type,
                        domain=domain,
                        idempotency_key=idempotency_key,
                    )
                report.persisted = True
                report.graph_durable = intent is not None and self._store_configured()
                self._persisted.add(key)
            except Exception as exc:  # noqa: BLE001 — fire-and-forward unless required
                if _is_durable_duplicate(exc):
                    # Another worker, or this process before a restart, already
                    # committed this operation: its result, event and durable
                    # graph obligation exist, and this commit rolled back whole.
                    report.skipped = True
                    self._persisted.add(key)
                    self._completed.add(key)
                    logger.info(
                        "side_effects_skipped_durable_duplicate", key=key, entity_id=entity_id
                    )
                    return report
                report.errors.append(f"persist:{exc}")
                logger.warning("side_effect_persist_failed", entity_id=entity_id, error=str(exc))
                if require_persistence:
                    # Raise before any downstream effect fires and before the key is
                    # recorded: an un-persisted result must leave no completion
                    # marker, or the retry is skipped as a duplicate.
                    logger.error(
                        "side_effect_persist_required_failed",
                        key=key,
                        entity_id=entity_id,
                        error=str(exc),
                    )
                    raise PersistenceRequiredError(
                        f"required persistence failed for {entity_id}: {exc}"
                    ) from exc

        # 2) Graph sync once (Gate-only PacketRouter), unless excluded.
        # `graph_synced` is set only on CEG's acknowledgment of THIS operation
        # (`graph_sync_acknowledged`): None, a failure or rejected answer, a
        # malformed answer, or an exception each leave it False. Anything else
        # recorded a sync the graph never confirmed (RV-DOMAIN-RD-07).
        if key in self._graph_leg_in_flight:
            # A detached leg for this key is already sending its sync. A second
            # one would double-sync the same operation; the in-flight leg owns
            # the outcome, and the key stays pending until it is acknowledged.
            report.graph_deferred = True
            logger.info("side_effect_graph_leg_already_in_flight", key=key, entity_id=entity_id)
        elif defer_graph_sync:
            # Not awaited here: the caller starts the leg detached once its own
            # answer is decided (`schedule_graph_leg`).
            report.graph_deferred = True
            logger.info("side_effect_graph_sync_deferred", key=key, entity_id=entity_id)
        elif not graph_sync:
            report.graph_skipped = True
            logger.info("side_effect_graph_sync_excluded", entity_id=entity_id, key=key)
            if graph_retry:
                # Excluding the leg is not acknowledging it. This call cannot
                # resolve the pending leg, so it is recorded and the key stays
                # pending below for a later call that does ask for the graph.
                report.errors.append("graph_sync:excluded_while_pending")
                logger.warning(
                    "side_effect_graph_sync_excluded_while_pending", key=key, entity_id=entity_id
                )
        else:
            if graph_entity_id != entity_id:
                # The semantic key does not carry entity_id (idem:{tenant}:{key}),
                # so a caller reusing an idempotency key across entities keyed
                # this call as a retry of another entity's operation. The leg
                # completes for the entity that was persisted and emitted; a
                # clean sync is not reported for this call's entity.
                report.errors.append(f"graph_sync:entity_mismatch:pinned={graph_entity_id}")
                logger.warning(
                    "side_effect_graph_retry_entity_mismatch",
                    key=key,
                    entity_id=entity_id,
                    pinned_entity_id=graph_entity_id,
                )
            await self._graph_leg(
                key=key,
                tenant=graph_tenant,
                entity_id=graph_entity_id,
                fields=synced_fields,
                domain=graph_domain,
                node_label=graph_node_label,
                contract_digest=graph_contract_digest,
                settings=settings,
                report=report,
            )

        # Event emit once, and never on a graph-only retry. Score invalidation
        # is not a step: Gate does not own score-invalidate, and a dispatch
        # without an acknowledgment must not set score_invalidated.
        if not graph_retry and emit_event:
            try:
                from app.services.event_emitter import get_emitter

                await get_emitter(settings).emit_enrichment_completed(
                    tenant_id=tenant,
                    entity_id=entity_id,
                    domain=domain,
                    fields=response_dict.get("fields", {}),
                    confidence=float(response_dict.get("confidence", 0.0) or 0.0),
                    tokens_used=int(response_dict.get("tokens_used", 0) or 0),
                )
                report.event_emitted = True
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"event:{exc}")
                logger.warning("side_effect_event_failed", entity_id=entity_id, error=str(exc))

        if key in self._completed:
            # Recovery resolved this key's durable intent while this call was
            # awaiting its event; that disposition stands, and no pending graph
            # leg is recorded beside it.
            self._graph_pending.pop(key, None)
            logger.info("side_effects_resolved_by_recovery", key=key, entity_id=entity_id)
            return report
        if (graph_sync or graph_retry or report.graph_deferred) and not _leg_resolved(report):
            # The graph leg was asked for (on this call, or on the earlier one
            # that left it pending) and did not complete. Recording the key as
            # completed would suppress the retry that can finish it, so the
            # key stays pending for the graph leg only (never re-persist, never
            # re-emit). A persist failure on this attempt is already reported
            # above and keeps its own semantics. The pending entry pins the
            # fields the retry must sync — those of the first attempt.
            self._graph_pending[key] = {
                "tenant": graph_tenant,
                "fields": dict(synced_fields),
                "domain": graph_domain,
                "node_label": graph_node_label,
                "contract_digest": graph_contract_digest,
            }
            log = logger.info if report.graph_deferred else logger.warning
            log(
                "side_effects_graph_pending",
                key=key,
                entity_id=entity_id,
                persisted=report.persisted,
                score_invalidated=report.score_invalidated,
                event_emitted=report.event_emitted,
                graph_retry=graph_retry,
                graph_deferred=report.graph_deferred,
            )
            # The entry also pins the entity those fields were persisted for: an
            # idem:{tenant}:{idempotency_key} key omits entity_id, so a retry can
            # carry another entity's id (recorded as entity_mismatch above).
            self._graph_pending[key]["entity_id"] = graph_entity_id
            return report

        self._graph_pending.pop(key, None)
        self._completed.add(key)
        logger.info(
            "side_effects_committed",
            key=key,
            entity_id=entity_id,
            persisted=report.persisted,
            graph_synced=report.graph_synced,
            graph_skipped=report.graph_skipped,
            graph_retry=graph_retry,
            graph_terminal=report.graph_terminal,
            score_invalidated=report.score_invalidated,
            event_emitted=report.event_emitted,
        )
        return report

    @staticmethod
    def _graph_pin(settings: Any, tenant: str, node_label: str | None) -> Any | None:
        """The endpoint the intent is pinned to now (A2), or None when unresolvable."""
        try:
            from app.engines.packet_router import GraphSyncPin, get_router

            pin = get_router(settings).graph_sync_pin(tenant, node_label=node_label)
        except Exception as exc:  # noqa: BLE001 — an unpinned intent is still durable
            logger.warning("side_effects_graph_pin_unavailable", tenant=tenant, error=str(exc))
            return None
        return pin if isinstance(pin, GraphSyncPin) else None


def _ack_summary(ack: Any) -> str:
    """Short, log-safe description of a graph-sync acknowledgment."""
    if ack is None:
        return "none"
    if not isinstance(ack, dict):
        return f"non-mapping:{type(ack).__name__}"
    return (
        f"status={ack.get('status')!r} synced_count={ack.get('synced_count')!r} "
        f"entity_type={ack.get('entity_type')!r}"
    )


_coordinator: SideEffectCoordinator | None = None
_coordinator_lock = threading.Lock()


def get_side_effect_coordinator() -> SideEffectCoordinator:
    global _coordinator
    if _coordinator is None:
        with _coordinator_lock:
            if _coordinator is None:
                _coordinator = SideEffectCoordinator()
    return _coordinator


def _is_durable_duplicate(exc: BaseException) -> bool:
    """`pg_store.GraphIntentDuplicateError`, matched without importing the DB runtime."""
    return type(exc).__name__ == "GraphIntentDuplicateError" and type(exc).__module__ == (
        "app.services.pg_store"
    )


def _leg_resolved(report: SideEffectReport) -> bool:
    """A graph leg that ended (synced or terminal) and was recorded durably."""
    return (report.graph_synced or report.graph_terminal) and not report.graph_unsettled


def _converge_graph_sync_enabled(settings: Any) -> bool:
    """`Settings.converge_graph_sync_enabled`; an object without it holds nothing."""
    value = getattr(settings, "converge_graph_sync_enabled", True)
    return value is not False


def _lease_expired(record: Any) -> bool:
    """Whether a claimed intent's lease ran out before its send started."""
    from datetime import UTC, datetime

    expires = getattr(record, "lease_expires_at", None)
    return isinstance(expires, datetime) and expires <= datetime.now(UTC)
