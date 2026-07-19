"""
Redis-backed idempotency layer.
Prevents duplicate enrichments when Salesforce or Odoo retries on timeout.
"""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as redis
import structlog

logger = structlog.get_logger("idempotency")


class IdempotencyStore:
    """Thin async Redis wrapper for idempotency keys."""

    def __init__(self, redis_url: str, ttl: int = 86400) -> None:
        self.client = redis.from_url(redis_url, decode_responses=True)
        self.ttl = ttl
        self.prefix = "enrich:idem:"

    async def get(self, key: str) -> dict[str, Any] | None:
        raw = await self.client.get(f"{self.prefix}{key}")
        if raw:
            logger.info("idempotency_hit", key=key)
            cached: dict[str, Any] = json.loads(raw)
            return cached
        return None

    async def set(self, key: str, response: dict[str, Any]) -> None:
        await self.client.set(
            f"{self.prefix}{key}",
            json.dumps(response, default=str),
            ex=self.ttl,
        )

    async def close(self) -> None:
        await self.client.aclose()
