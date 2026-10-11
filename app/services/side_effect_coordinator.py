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

import hashlib
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
) -> str:
    """Build a stable semantic key (packet id preferred, else idempotency, else hash)."""
    if packet_id:
        return f"pkt:{packet_id}:{action}"
    if idempotency_key:
        return f"idem:{tenant}:{idempotency_key}:{action}"
    digest = hashlib.sha256(f"{tenant}|{entity_id}|{action}".encode()).hexdigest()[:24]
    return f"hash:{digest}"


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
      the sync without duplicating effects that already happened.

    A key is never in both. Both are process-local, like the guard always was.
    """

    def __init__(self) -> None:
        self._completed: set[str] = set()
        self._graph_pending: set[str] = set()

    def reset_for_tests(self) -> None:
        self._completed.clear()
        self._graph_pending.clear()

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
    ) -> SideEffectReport:
        """Commit post-enrich side effects once per semantic key.

        `graph_sync=False` excludes the Gate->GRAPH round trip, and ONLY that.
        A caller that is itself answering a latency-bounded request uses it:
        `notify_graph_sync` awaits up to three Gate attempts at 30 s each with
        backoff between, which no synchronous caller budget can absorb.

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
        """
        key = semantic_side_effect_key(
            tenant=tenant,
            entity_id=entity_id,
            action="enrich",
            packet_id=packet_id,
            idempotency_key=idempotency_key,
        )
        report = SideEffectReport(key=key)
        if key in self._completed:
            report.skipped = True
            logger.info("side_effects_skipped_duplicate", key=key, entity_id=entity_id)
            return report

        # A key left pending by an unacknowledged graph sync re-runs the graph
        # leg only: persistence, score invalidation and the completion event
        # already happened for it and must not happen twice.
        graph_retry = key in self._graph_pending
        report.graph_retry = graph_retry
        if graph_retry:
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
        else:
            try:
                from app.engines.packet_router import get_router, graph_sync_acknowledged

                router = get_router(settings)
                ack = await router.notify_graph_sync(
                    tenant_id=tenant,
                    entity_id=entity_id,
                    fields=response_dict.get("fields", {}),
                    domain=domain,
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

        if graph_sync and not report.graph_synced:
            # The graph leg was asked for and did not complete. Recording the key
            # as completed would suppress the retry that can finish it, so the
            # key stays pending for the graph leg only (never re-persist, never
            # re-emit). A persist failure on this attempt is already reported
            # above and keeps its own semantics.
            self._graph_pending.add(key)
            logger.warning(
                "side_effects_graph_pending",
                key=key,
                entity_id=entity_id,
                persisted=report.persisted,
                score_invalidated=report.score_invalidated,
                event_emitted=report.event_emitted,
                graph_retry=graph_retry,
            )
            return report

        self._graph_pending.discard(key)
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
