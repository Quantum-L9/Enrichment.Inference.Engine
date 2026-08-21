"""Tenant-scoped Redis idempotency (v2).

Identity is (tenant, action, idempotency_key) plus a request fingerprint.
Raw tenant IDs and raw keys never appear in Redis key names or INFO logs.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import redis.asyncio as redis
import structlog

logger = structlog.get_logger("idempotency")

CACHE_VERSION = 2
KEY_PREFIX = "enrich:idem:v2:"


class IdempotencyConflict(Exception):
    """Same tenant + key, different semantic request."""


class IdempotencyStore:
    """Async Redis wrapper for tenant-scoped idempotency keys."""

    def __init__(self, redis_url: str, ttl: int = 86400) -> None:
        self.client = redis.from_url(redis_url, decode_responses=True)
        self.ttl = ttl
        self.prefix = KEY_PREFIX

    def _namespaced_key(self, tenant: str, key: str) -> str:
        digest = hashlib.sha256(f"{tenant}\0{key}".encode()).hexdigest()
        return f"{self.prefix}{digest}"

    async def get(
        self,
        key: str,
        tenant: str | None = None,
        fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        if tenant is None or fingerprint is None:
            return None
        raw = await self.client.get(self._namespaced_key(tenant, key))
        if not raw:
            return None
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
            return None
        stored_fp = payload.get("request_fingerprint")
        if stored_fp != fingerprint:
            logger.warning("idempotency_conflict")
            raise IdempotencyConflict("idempotency key reused with a different request")
        logger.debug("idempotency_hit")
        response = payload.get("response")
        return response if isinstance(response, dict) else None

    async def set(
        self,
        key: str,
        response: dict[str, Any],
        tenant: str | None = None,
        fingerprint: str | None = None,
    ) -> None:
        if tenant is None or fingerprint is None:
            return
        envelope = {
            "version": CACHE_VERSION,
            "request_fingerprint": fingerprint,
            "response": response,
        }
        await self.client.set(
            self._namespaced_key(tenant, key),
            json.dumps(envelope, default=str),
            ex=self.ttl,
        )

    async def close(self) -> None:
        await self.client.aclose()
