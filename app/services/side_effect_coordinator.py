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

Two things this module is careful about, because collapsing either one is how a
clean domain contract acquires a hidden persistence hole.

**Required vs optional.** "Side effect" is not one category. Enrichment result
persistence is REQUIRED: the response a caller holds is a claim that the result
is durable, so a failure to commit it falsifies the answer. Graph
synchronisation, score invalidation and event emission are OPTIONAL: their
failure is observable and retryable out of band, and does not make the returned
enrichment wrong. A caller declares which contract it wants via
``require_persistence``; only the required one raises.

**Logical operation identity vs packet identity.** Deduplication is keyed on the
LOGICAL operation — ``tenant`` + caller-supplied ``idempotency_key`` — never on
an entity, and never primarily on a transport packet. A transport replay carries
a new ``packet_id`` for the same logical operation, so packet identity cannot
establish that two attempts are the same operation; and an entity-scoped key
would make every future enrichment of that entity look like a duplicate of the
first, silently discarding legitimate re-runs. When no logical identity is
available the honest answer is *no completion dedupe at all* (``None``), not a
fabricated one — see ``semantic_side_effect_key``.

The durable database boundary in ``pg_store`` is the authority for replay
safety. The per-key lock here only stops two in-process callers doing the same
work concurrently; it is an optimisation, never the system invariant.

All egress is Gate-only via PacketRouter / event emitter.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger("side_effect_coordinator")


class RequiredSideEffectError(RuntimeError):
    """A side effect the canonical contract requires did not commit.

    Raised only when the caller asked for the required-persistence contract.
    Its meaning to the handler is precise: the operation did NOT complete, no
    completion may be acknowledged or recorded, and a retry is still correct.
    """


def semantic_side_effect_key(
    *,
    tenant: str,
    entity_id: str,
    action: str = "enrich",
    packet_id: str | None = None,
    idempotency_key: str | None = None,
) -> str | None:
    """Identity of the LOGICAL operation, or None when there isn't one.

    Priority, and why:

    1. ``tenant`` + ``idempotency_key`` — the caller's own statement that two
       requests are the same logical operation. Tenant-scoped because a raw
       caller key is not globally unique: two tenants may legitimately send the
       same string, and they are different operations.
    2. ``tenant`` + ``packet_id`` — a single transport attempt. This CANNOT
       recognise a replay (a retry carries a new packet id), so it is only a
       guard against committing one packet's effects twice. Never primary.
    3. ``None`` — no logical identity was supplied. Returning None means "run
       the effects, record no completion". The alternative the old code took —
       hashing ``tenant|entity|action`` — made the FIRST enrichment of an entity
       permanently mark every later one a duplicate, which is not idempotency
       but data loss.

    ``entity_id`` is accepted for logging symmetry only; it deliberately never
    contributes to the key.
    """
    if idempotency_key and idempotency_key.strip():
        return f"idem:{tenant}:{idempotency_key.strip()}:{action}"
    if packet_id and packet_id.strip():
        return f"pkt:{tenant}:{packet_id.strip()}:{action}"
    return None


@dataclass
class SideEffectReport:
    key: str | None
    persisted: bool = False
    graph_synced: bool = False
    graph_skipped: bool = False
    score_invalidated: bool = False
    event_emitted: bool = False
    skipped: bool = False
    deduped: bool = True
    errors: list[str] = field(default_factory=list)


class SideEffectCoordinator:
    """Once-per-logical-operation coordinator for enrich follow-up effects."""

    def __init__(self) -> None:
        self._completed: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = threading.Lock()

    def reset_for_tests(self) -> None:
        self._completed.clear()
        self._locks.clear()

    def _lock_for(self, key: str) -> asyncio.Lock:
        """Per-key lock so two concurrent callers do not duplicate the work.

        This serialises same-key execution inside one process. It is not the
        correctness boundary for replay — the database unique constraint on
        (tenant_id, idempotency_key) is — because a second process, or a
        restarted one, shares no lock with this one.
        """
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

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
        """Commit post-enrich side effects once per logical operation.

        `graph_sync=False` excludes the Gate->GRAPH round trip. A caller that is
        itself answering a latency-bounded request uses it: `notify_graph_sync`
        awaits up to three Gate attempts at 30 s each with backoff between, which
        no synchronous caller budget can absorb.

        `require_persistence=True` raises `RequiredSideEffectError` when the
        enrichment result does not commit, and records no completion for the
        key. Optional effects never raise, and their failure never blocks the
        completion marker — they are observable in the report and the log.
        """
        key = semantic_side_effect_key(
            tenant=tenant,
            entity_id=entity_id,
            action="enrich",
            packet_id=packet_id,
            idempotency_key=idempotency_key,
        )

        if key is None:
            # No logical identity: run the effects, mark nothing. Persistence
            # stays correct because pg_store writes a row per run when there is
            # no key to replay against — which is the right domain semantics for
            # "the caller did not tell us these were the same operation".
            logger.info(
                "side_effects_no_logical_identity",
                entity_id=entity_id,
                tenant=tenant,
                detail="no idempotency_key or packet_id; completion dedupe disabled",
            )
            return await self._run_effects(
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
                key=None,
                record_completion=False,
            )

        async with self._lock_for(key):
            if key in self._completed:
                report = SideEffectReport(key=key, skipped=True)
                logger.info("side_effects_skipped_duplicate", key=key, entity_id=entity_id)
                return report

            report = await self._run_effects(
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
                key=key,
                record_completion=True,
            )
            return report

    async def _run_effects(
        self,
        *,
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
        key: str | None,
        record_completion: bool,
    ) -> SideEffectReport:
        report = SideEffectReport(key=key, deduped=record_completion)

        # 1) REQUIRED (when the caller says so): persist the enrichment result.
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
        except Exception as exc:
            report.errors.append(f"persist:{exc}")
            if require_persistence:
                # Fail closed. No completion marker is written, so a retry of
                # this same logical operation is not suppressed.
                logger.error(
                    "side_effect_required_persist_failed",
                    entity_id=entity_id,
                    key=key,
                    error=str(exc),
                )
                raise RequiredSideEffectError(
                    f"required enrichment persistence failed for {entity_id}: {exc}"
                ) from exc
            logger.warning("side_effect_persist_failed", entity_id=entity_id, error=str(exc))

        # 2) OPTIONAL: graph sync once (Gate-only PacketRouter), unless excluded.
        if not graph_sync:
            report.graph_skipped = True
            logger.info("side_effect_graph_sync_excluded", entity_id=entity_id, key=key)
        else:
            try:
                from app.engines.packet_router import get_router

                router = get_router(settings)
                await router.notify_graph_sync(
                    tenant_id=tenant,
                    entity_id=entity_id,
                    fields=response_dict.get("fields", {}),
                    domain=domain,
                )
                report.graph_synced = True
            except Exception as exc:  # noqa: BLE001 — optional effect
                report.errors.append(f"graph_sync:{exc}")
                logger.warning("side_effect_graph_sync_failed", entity_id=entity_id, error=str(exc))

        # 3) OPTIONAL: score invalidation once.
        try:
            from app.engines.packet_router import get_router

            router = get_router(settings)
            await router.notify_score_invalidate(
                tenant_id=tenant,
                entity_id=entity_id,
                domain=domain,
            )
            report.score_invalidated = True
        except Exception as exc:  # noqa: BLE001 — optional effect
            report.errors.append(f"score_invalidate:{exc}")
            logger.warning(
                "side_effect_score_invalidate_failed", entity_id=entity_id, error=str(exc)
            )

        # 4) OPTIONAL: event emit once.
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
            except Exception as exc:  # noqa: BLE001 — optional effect
                report.errors.append(f"event:{exc}")
                logger.warning("side_effect_event_failed", entity_id=entity_id, error=str(exc))

        # The completion marker means "the required obligations for this logical
        # operation are satisfied", not "something ran". It is written only when
        # persistence actually committed — an unpersisted run must stay
        # retryable — and only when there is a logical key to mark.
        if record_completion and key is not None and report.persisted:
            self._completed.add(key)

        logger.info(
            "side_effects_committed",
            key=key,
            entity_id=entity_id,
            persisted=report.persisted,
            graph_synced=report.graph_synced,
            graph_skipped=report.graph_skipped,
            score_invalidated=report.score_invalidated,
            event_emitted=report.event_emitted,
            deduped=report.deduped,
        )
        return report


_coordinator: SideEffectCoordinator | None = None
_coordinator_lock = threading.Lock()


def get_side_effect_coordinator() -> SideEffectCoordinator:
    global _coordinator
    if _coordinator is None:
        with _coordinator_lock:
            if _coordinator is None:
                _coordinator = SideEffectCoordinator()
    return _coordinator
