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

Guarantees at most one persistence, one score invalidation, and one
enrichment-completed event per semantic request key, and records the graph
sync as done only once CEG acknowledges it — an unacknowledged graph leg is
the one effect a retry of the same key may attempt again (RV-DOMAIN-RD-07).
All egress is Gate-only via PacketRouter / event emitter.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Any

import structlog

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

    Preference: the transport packet id (``packet.header.packet_id``, immutable
    and signed — never a payload field, D-7), else the caller's idempotency key,
    else the result itself. The result digest stands in for an operation
    identity when the caller supplied none: two runs for one entity that
    produced different results are different operations and persist twice,
    while a byte-identical result is the same effect and persists once. The
    legacy tenant|entity|action hash (no digest) collapsed every run of an
    entity into one key, so the second run was skipped and never persisted.
    """
    if packet_id:
        return f"pkt:{packet_id}:{action}"
    if idempotency_key:
        return f"idem:{tenant}:{idempotency_key}:{action}"
    material = f"{tenant}|{entity_id}|{action}"
    if result_digest:
        digest = hashlib.sha256(f"{material}|{result_digest}".encode()).hexdigest()[:24]
        return f"res:{digest}"
    digest = hashlib.sha256(material.encode()).hexdigest()[:24]
    return f"hash:{digest}"


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
    score_invalidated: bool = False
    event_emitted: bool = False
    skipped: bool = False
    # True when this call re-attempted ONLY the Gate->GRAPH leg of a key whose
    # other effects already completed on an earlier attempt (RV-DOMAIN-RD-07).
    graph_retry: bool = False
    errors: list[str] = field(default_factory=list)


class SideEffectCoordinator:
    """Process-local once-per-key coordinator for enrich follow-up effects.

    Two markers, one key space:

    * ``_completed`` — every effect the call asked for either ran or was
      explicitly excluded. A later call for the key is a duplicate and skips.
    * ``_graph_pending`` — persistence, score invalidation and the completion
      event ran, but CEG did not acknowledge the graph sync. A later call for
      the key re-attempts the graph leg ONLY, so a legitimate retry can finish
      the sync without duplicating effects that already happened. The entry
      holds the ``entity_id``, ``fields`` and ``domain`` that WERE persisted and
      emitted, and the retry sends exactly those: the graph must end up agreeing with the
      store and the event, not with whatever the retrying call happens to carry
      (a later enrichment of the same entity can key identically under the
      tenant/entity fallback, or re-run with a different LLM answer).

    A key is never in both. Both are process-local, like the guard always was.
    """

    def __init__(self) -> None:
        self._completed: set[str] = set()
        # Keys whose result a store accepted. A key can be completed without
        # it when persistence was not required and failed (F-001).
        self._persisted: set[str] = set()
        self._graph_pending: dict[str, dict[str, Any]] = {}
        # Reserved before the first await (F-024): a concurrent call for the
        # same key waits for the holder and then re-evaluates.
        self._in_flight: dict[str, asyncio.Event] = {}

    def reset_for_tests(self) -> None:
        self._completed.clear()
        self._persisted.clear()
        self._graph_pending.clear()
        self._in_flight.clear()

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
    ) -> SideEffectReport:
        """Commit post-enrich side effects once per semantic key.

        `graph_sync=False` excludes the Gate->GRAPH round trip, and ONLY that.
        A caller that is itself answering a latency-bounded request uses it:
        `notify_graph_sync` awaits up to two Gate attempts at 10 s each with a
        1 s backoff between (packet_router._MAX_ATTEMPTS / _ROUTE_TIMEOUT) --
        21 s worst case, which a 30 s synchronous caller budget cannot absorb
        on top of the enrichment itself.

        EIE-004 read the score-invalidation call below as escaping this guard.
        It does not need the guard, and the distinction is the reason the flag
        is named `graph_sync` rather than `side_effects`:

        * `notify_graph_sync` is **awaited**, and goes to GRAPH (CEG's `sync`).
          Its latency lands on the caller. That is what the flag excludes.
        * `notify_score_invalidate` is **fire-and-forget** (`route_fire_and_forget`
          schedules a task and returns), and goes to SCORE, a different node. It
          adds no measurable time to a bounded path, and suppressing it would
          leave a stale score behind for an entity that was just enriched —
          a correctness loss bought for no latency.

        So a bounded caller still invalidates scores. Anything that becomes an
        awaited Gate hop must move inside the guard, or take a flag of its own.

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
    ) -> SideEffectReport:
        """Run the effects for a key this call holds the reservation for."""
        report = SideEffectReport(key=key)

        # A key left pending by an unacknowledged graph sync re-runs the graph
        # leg only: persistence, score invalidation and the completion event
        # already happened for it and must not happen twice.
        pending = self._graph_pending.get(key)
        graph_retry = pending is not None
        report.graph_retry = graph_retry
        # What the graph leg syncs: on a retry, the entity, fields and domain
        # that were persisted and emitted on the first attempt, never this call's.
        graph_entity_id = entity_id
        graph_fields: dict[str, Any] = response_dict.get("fields", {})
        graph_domain = domain
        graph_node_label = node_label
        if pending is not None:
            graph_entity_id = pending["entity_id"]
            graph_fields = pending["fields"]
            graph_domain = pending["domain"]
            graph_node_label = pending.get("node_label")
            logger.info("side_effects_graph_retry", key=key, entity_id=entity_id)

        # 1) Persist once
        if not graph_retry:
            try:
                from app.models.schemas import EnrichResponse
                from app.services.result_store import ResultStore

                store = ResultStore(tenant_id=tenant)
                resp_obj = EnrichResponse.model_validate(response_dict)
                await store.persist_enrich_response(
                    response=resp_obj,
                    entity_id=entity_id,
                    object_type=object_type,
                    domain=domain,
                    idempotency_key=idempotency_key,
                )
                report.persisted = True
                self._persisted.add(key)
            except Exception as exc:  # noqa: BLE001 — fire-and-forward unless required
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
        if not graph_sync:
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
            try:
                from app.engines.packet_router import get_router, graph_sync_acknowledged

                router = get_router(settings)
                ack = await router.notify_graph_sync(
                    tenant_id=tenant,
                    entity_id=graph_entity_id,
                    fields=graph_fields,
                    domain=graph_domain,
                    node_label=graph_node_label,
                )
                if graph_sync_acknowledged(ack):
                    report.graph_synced = True
                else:
                    report.errors.append(f"graph_sync:unacknowledged:{_ack_summary(ack)}")
                    logger.warning(
                        "side_effect_graph_sync_unacknowledged",
                        entity_id=entity_id,
                        key=key,
                        ack=_ack_summary(ack),
                    )
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"graph_sync:{exc}")
                logger.warning("side_effect_graph_sync_failed", entity_id=entity_id, error=str(exc))

        if not graph_retry:
            # 3) Score invalidation once — deliberately OUTSIDE the graph_sync guard.
            # Fire-and-forget to SCORE, not an awaited hop to GRAPH: it costs a
            # bounded caller nothing, and skipping it would leave a stale score on an
            # entity that was just enriched. See the docstring (EIE-004).
            try:
                from app.engines.packet_router import get_router

                router = get_router(settings)
                await router.notify_score_invalidate(
                    tenant_id=tenant,
                    entity_id=entity_id,
                    domain=domain,
                )
                report.score_invalidated = True
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"score_invalidate:{exc}")
                logger.warning(
                    "side_effect_score_invalidate_failed", entity_id=entity_id, error=str(exc)
                )

            # 4) Event emit once
            if emit_event:
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

        if (graph_sync or graph_retry) and not report.graph_synced:
            # The graph leg was asked for (on this call, or on the earlier one
            # that left it pending) and did not complete. Recording the key as
            # completed would suppress the retry that can finish it, so the
            # key stays pending for the graph leg only (never re-persist, never
            # re-emit). A persist failure on this attempt is already reported
            # above and keeps its own semantics. The pending entry pins the
            # fields the retry must sync — those of the first attempt.
            self._graph_pending[key] = {
                "fields": graph_fields,
                "domain": graph_domain,
                "node_label": graph_node_label,
            }
            logger.warning(
                "side_effects_graph_pending",
                key=key,
                entity_id=entity_id,
                persisted=report.persisted,
                score_invalidated=report.score_invalidated,
                event_emitted=report.event_emitted,
                graph_retry=graph_retry,
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
            score_invalidated=report.score_invalidated,
            event_emitted=report.event_emitted,
        )
        return report


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
