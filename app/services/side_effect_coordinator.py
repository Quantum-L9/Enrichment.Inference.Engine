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

Guarantees at most one persistence, one graph-sync dispatch, one score
invalidation, and one enrichment-completed event per semantic request key.
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


@dataclass
class SideEffectReport:
    key: str
    persisted: bool = False
    graph_synced: bool = False
    graph_skipped: bool = False
    score_invalidated: bool = False
    event_emitted: bool = False
    skipped: bool = False
    errors: list[str] = field(default_factory=list)


class SideEffectCoordinator:
    """Process-local once-per-key coordinator for enrich follow-up effects."""

    def __init__(self) -> None:
        self._completed: set[str] = set()

    def reset_for_tests(self) -> None:
        self._completed.clear()

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
    ) -> SideEffectReport:
        """Commit post-enrich side effects once per semantic key.

        `graph_sync=False` excludes the Gate->GRAPH round trip. A caller that is
        itself answering a latency-bounded request uses it: `notify_graph_sync`
        awaits up to three Gate attempts at 30 s each with backoff between, which
        no synchronous caller budget can absorb.
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

        # 1) Persist once
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
        except Exception as exc:  # noqa: BLE001 — fire-and-forward
            report.errors.append(f"persist:{exc}")
            logger.warning("side_effect_persist_failed", entity_id=entity_id, error=str(exc))

        # 2) Graph sync once (Gate-only PacketRouter), unless excluded
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
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"graph_sync:{exc}")
                logger.warning("side_effect_graph_sync_failed", entity_id=entity_id, error=str(exc))

        # 3) Score invalidation once
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
