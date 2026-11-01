"""Tests for app.services.event_emitter — fire-and-forget event publishing."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.event_emitter import (
    EnrichmentEvent,
    EventEmitter,
    EventType,
    RedisStreamsBackend,
    get_emitter,
)


@pytest.fixture
def mock_redis_backend() -> AsyncMock:
    backend = AsyncMock(spec=RedisStreamsBackend)
    backend.publish = AsyncMock()
    backend.close = AsyncMock()
    return backend


@pytest.fixture
def emitter(mock_redis_backend: AsyncMock) -> EventEmitter:
    return EventEmitter(backend=mock_redis_backend)


@pytest.fixture
def sample_event() -> EnrichmentEvent:
    return EnrichmentEvent(
        event_type=EventType.ENRICHMENT_COMPLETED,
        entity_id="ent-001",
        tenant_id="tenant-abc",
        domain="plastics_recycling",
        payload={"fields_count": 5, "confidence": 0.85, "tokens_used": 1200},
    )


class TestEnrichmentEvent:
    def test_to_stream_dict_keys(self, sample_event: EnrichmentEvent) -> None:
        d = sample_event.to_stream_dict()
        expected_keys = {
            "event_type",
            "entity_id",
            "tenant_id",
            "domain",
            "correlation_id",
            "occurred_at",
            "payload",
        }
        assert set(d.keys()) == expected_keys

    def test_to_stream_dict_values_are_strings(self, sample_event: EnrichmentEvent) -> None:
        d = sample_event.to_stream_dict()
        for v in d.values():
            assert isinstance(v, str)

    def test_event_type_serializes(self, sample_event: EnrichmentEvent) -> None:
        d = sample_event.to_stream_dict()
        assert d["event_type"] == "enrichment_completed"


class TestRedisStreamsBackend:
    @pytest.mark.asyncio
    async def test_publish_preserves_xadd_stream_arguments(
        self, sample_event: EnrichmentEvent
    ) -> None:
        redis_client = AsyncMock()
        with patch("redis.asyncio.from_url", return_value=redis_client):
            backend = RedisStreamsBackend("redis://localhost:6379")

        await backend.publish(sample_event)

        redis_client.xadd.assert_awaited_once_with(
            "enrich:events:tenant-abc",
            sample_event.to_stream_dict(),
            maxlen=10_000,
            approximate=True,
        )


class TestEventEmitter:
    @pytest.mark.asyncio
    async def test_emit_calls_backend_publish(
        self,
        emitter: EventEmitter,
        mock_redis_backend: AsyncMock,
        sample_event: EnrichmentEvent,
    ) -> None:
        await emitter.emit(sample_event)
        await asyncio.sleep(0.05)
        mock_redis_backend.publish.assert_called_once_with(sample_event)

    @pytest.mark.asyncio
    async def test_emit_swallows_backend_error(
        self,
        emitter: EventEmitter,
        mock_redis_backend: AsyncMock,
        sample_event: EnrichmentEvent,
    ) -> None:
        mock_redis_backend.publish.side_effect = ConnectionError("Redis down")
        await emitter.emit(sample_event)
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_emit_swallows_timeout(
        self,
        emitter: EventEmitter,
        mock_redis_backend: AsyncMock,
        sample_event: EnrichmentEvent,
    ) -> None:
        async def slow_publish(_: EnrichmentEvent) -> None:
            await asyncio.sleep(10)

        mock_redis_backend.publish = slow_publish
        await emitter.emit(sample_event)
        await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_emit_enrichment_completed(
        self,
        emitter: EventEmitter,
        mock_redis_backend: AsyncMock,
    ) -> None:
        await emitter.emit_enrichment_completed(
            tenant_id="t1",
            entity_id="e1",
            domain="plastics",
            fields={"polymer_type": "HDPE"},
            confidence=0.9,
            tokens_used=500,
        )
        await asyncio.sleep(0.05)
        mock_redis_backend.publish.assert_called_once()
        event = mock_redis_backend.publish.call_args[0][0]
        assert event.event_type == EventType.ENRICHMENT_COMPLETED
        assert event.entity_id == "e1"

    @pytest.mark.asyncio
    async def test_emit_convergence_completed(
        self,
        emitter: EventEmitter,
        mock_redis_backend: AsyncMock,
    ) -> None:
        await emitter.emit_convergence_completed(
            tenant_id="t1",
            entity_id="e1",
            domain="plastics",
            pass_count=3,
            convergence_reason="threshold_met",
            total_tokens=5000,
            total_cost_usd=0.025,
        )
        await asyncio.sleep(0.05)
        event = mock_redis_backend.publish.call_args[0][0]
        assert event.event_type == EventType.CONVERGENCE_COMPLETED

    @pytest.mark.asyncio
    async def test_emit_schema_proposed(
        self,
        emitter: EventEmitter,
        mock_redis_backend: AsyncMock,
    ) -> None:
        await emitter.emit_schema_proposed(
            tenant_id="t1",
            domain="plastics",
            batch_run_id="batch-001",
            proposals_count=3,
        )
        await asyncio.sleep(0.05)
        event = mock_redis_backend.publish.call_args[0][0]
        assert event.event_type == EventType.SCHEMA_PROPOSED


class TestGetEmitter:
    def test_get_emitter_returns_emitter(self) -> None:
        settings = MagicMock()
        settings.redis_url = "redis://localhost:6379"
        emitter = get_emitter(settings)
        assert isinstance(emitter, EventEmitter)
