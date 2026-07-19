"""
app/services/event_emitter.py

Event emitter — publishes enrichment lifecycle events to downstream nodes.

Backends:
    RedisStreamsBackend — XADD to tenant-scoped Redis Streams (default)
    NATSBackend — publish to NATS subject (optional, requires nats-py)

All emit calls are fire-and-forget via asyncio.create_task.
The hot enrichment path is never blocked by event delivery.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from typing import Any, cast

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger("event_emitter")

_REDIS_STREAM_MAXLEN = 10_000


class EventType(StrEnum):
    ENRICHMENT_COMPLETED = "enrichment_completed"
    ENRICHMENT_FAILED = "enrichment_failed"
    CONVERGENCE_COMPLETED = "convergence_completed"
    SCHEMA_PROPOSED = "schema_proposed"
    SCORE_INVALIDATED = "score_invalidated"
    ENTITY_UPDATED = "entity_updated"


class EnrichmentEvent(BaseModel):
    event_type: EventType
    entity_id: str
    tenant_id: str
    domain: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_stream_dict(self) -> dict[str, str]:
        return {
            "event_type": self.event_type.value,
            "entity_id": self.entity_id,
            "tenant_id": self.tenant_id,
            "domain": self.domain or "",
            "correlation_id": str(self.correlation_id),
            "occurred_at": self.occurred_at.isoformat(),
            "payload": json.dumps(self.payload),
        }


class RedisStreamsBackend:
    """Publishes events to Redis Streams. Stream key: enrich:events:{tenant_id}"""

    def __init__(self, redis_url: str) -> None:
        import redis.asyncio as aioredis

        self._client = aioredis.from_url(redis_url, decode_responses=True)

    async def publish(self, event: EnrichmentEvent) -> None:
        stream_key = f"enrich:events:{event.tenant_id}"
        # cast: to_stream_dict() returns dict[str, str], which is a valid xadd
        # field mapping at runtime; the cast only bridges redis-py's invariant
        # key/value union typing.
        await self._client.xadd(
            stream_key,
            cast("dict[Any, Any]", event.to_stream_dict()),
            maxlen=_REDIS_STREAM_MAXLEN,
            approximate=True,
        )
        logger.debug(
            "event_published_redis",
            stream=stream_key,
            event_type=event.event_type.value,
            entity_id=event.entity_id,
        )

    async def close(self) -> None:
        await self._client.aclose()


class NATSBackend:
    """Publishes events to NATS subjects. Requires nats-py."""

    def __init__(self, nats_url: str) -> None:
        self._nats_url = nats_url
        self._nc: Any = None

    async def connect(self) -> None:
        try:
            import nats

            self._nc = await nats.connect(self._nats_url)
        except ImportError as exc:
            raise RuntimeError("nats-py not installed. Run: pip install nats-py") from exc

    async def publish(self, event: EnrichmentEvent) -> None:
        if self._nc is None:
            await self.connect()
        subject = f"enrich.events.{event.tenant_id}.{event.event_type.value}"
        payload = json.dumps(event.model_dump(mode="json")).encode()
        await self._nc.publish(subject, payload)

    async def close(self) -> None:
        if self._nc:
            await self._nc.close()


class EventEmitter:
    """Fire-and-forget event publisher. Never blocks the enrichment hot path."""

    def __init__(self, backend: RedisStreamsBackend | NATSBackend) -> None:
        self._backend = backend

    async def _publish_safe(self, event: EnrichmentEvent) -> None:
        try:
            await asyncio.wait_for(self._backend.publish(event), timeout=5.0)
        except TimeoutError:
            logger.warning(
                "event_emit_timeout",
                event_type=event.event_type.value,
                entity_id=event.entity_id,
            )
        except (ConnectionError, OSError) as exc:
            logger.warning(
                "event_emit_connection_error",
                event_type=event.event_type.value,
                entity_id=event.entity_id,
                error=str(exc),
            )
        except Exception as exc:
            logger.warning(
                "event_emit_failed",
                event_type=event.event_type.value,
                entity_id=event.entity_id,
                error=str(exc),
                exc_info=True,
            )

    async def emit(self, event: EnrichmentEvent) -> None:
        """Fire-and-forget. Returns immediately; never raises."""
        asyncio.create_task(self._publish_safe(event))

    async def emit_enrichment_completed(
        self,
        tenant_id: str,
        entity_id: str,
        domain: str | None,
        fields: dict[str, Any],
        confidence: float,
        tokens_used: int,
    ) -> None:
        await self.emit(
            EnrichmentEvent(
                event_type=EventType.ENRICHMENT_COMPLETED,
                entity_id=entity_id,
                tenant_id=tenant_id,
                domain=domain,
                payload={
                    "fields_count": len(fields),
                    "confidence": confidence,
                    "tokens_used": tokens_used,
                },
            )
        )

    async def emit_convergence_completed(
        self,
        tenant_id: str,
        entity_id: str,
        domain: str | None,
        pass_count: int,
        convergence_reason: str,
        total_tokens: int,
        total_cost_usd: float,
    ) -> None:
        await self.emit(
            EnrichmentEvent(
                event_type=EventType.CONVERGENCE_COMPLETED,
                entity_id=entity_id,
                tenant_id=tenant_id,
                domain=domain,
                payload={
                    "pass_count": pass_count,
                    "convergence_reason": convergence_reason,
                    "total_tokens": total_tokens,
                    "total_cost_usd": total_cost_usd,
                },
            )
        )

    async def emit_schema_proposed(
        self,
        tenant_id: str,
        domain: str,
        batch_run_id: str,
        proposals_count: int,
    ) -> None:
        await self.emit(
            EnrichmentEvent(
                event_type=EventType.SCHEMA_PROPOSED,
                entity_id=batch_run_id,
                tenant_id=tenant_id,
                domain=domain,
                payload={"batch_run_id": batch_run_id, "proposals_count": proposals_count},
            )
        )


@lru_cache(maxsize=1)
def get_emitter_cached(redis_url: str) -> EventEmitter:
    backend = RedisStreamsBackend(redis_url=redis_url)
    return EventEmitter(backend=backend)


def get_emitter(settings: Any) -> EventEmitter:
    """Module-level singleton factory. Cached per process."""
    return get_emitter_cached(settings.redis_url)
