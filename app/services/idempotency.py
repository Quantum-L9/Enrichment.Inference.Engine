"""
Redis-backed idempotency layer.
Prevents duplicate enrichments when Salesforce or Odoo retries on timeout.

An idempotency key is chosen by the caller, so two callers choosing the same
string is ordinary. A binding is therefore owned by a *scope* — the identity
the chassis authenticated — and by the request it was made for (EIE-FIX-001):

* the Redis key is derived from (scope, idempotency_key), so one tenant's key
  can never resolve to another tenant's result;
* the stored record carries a fingerprint of the request, so the same key
  reused inside one scope for a different request is refused, never replayed;
* the first completed request binds the key (SET NX); a later write never
  replaces it.

Scope and key are hashed into the Redis key, so neither tenant names nor
caller keys are readable from a key listing or from these logs. Records written
under the pre-scope `enrich:idem:{key}` layout are never read by this code and
expire on their original TTL.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import redis.asyncio as redis
import structlog

logger = structlog.get_logger("idempotency")

KEY_PREFIX = "enrich:idem:v2:"


class IdempotencyConflictError(Exception):
    """The key is already bound, in this scope, to a different request."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def tenant_scope(tenant: str) -> str:
    """Scope for a request whose tenant the SDK runtime resolved (C-02)."""
    return f"tenant:{tenant}"


def api_key_scope(api_key: str) -> str:
    """Scope for the HTTP surface, whose only identity is the authenticated key."""
    return f"api-key:{_digest(api_key)}"


def request_fingerprint(request: Mapping[str, Any]) -> str:
    """Stable digest of the request a key is bound to (key itself excluded by the caller)."""
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
    return _digest(canonical)


class IdempotencyStore:
    """Thin async Redis wrapper for idempotency keys."""

    def __init__(self, redis_url: str, ttl: int = 86400) -> None:
        self.client = redis.from_url(redis_url, decode_responses=True)
        self.ttl = ttl
        self.prefix = KEY_PREFIX

    def _redis_key(self, scope: str, key: str) -> str:
        return f"{self.prefix}{_digest(scope)}:{_digest(key)}"

    async def get(self, scope: str, key: str, fingerprint: str) -> dict[str, Any] | None:
        """Return the response bound to (scope, key) for this exact request.

        Raises IdempotencyConflictError when the key is bound, in this scope,
        to a different request. An unreadable record is a miss, never a replay.
        """
        raw = await self.client.get(self._redis_key(scope, key))
        if not raw:
            return None
        try:
            record = json.loads(raw)
            bound_to = record["fingerprint"]
            response = record["response"]
        except (ValueError, KeyError, TypeError):
            logger.warning("idempotency_record_unreadable", key_digest=_digest(key)[:12])
            return None
        if bound_to != fingerprint:
            logger.warning("idempotency_conflict", key_digest=_digest(key)[:12])
            raise IdempotencyConflictError(
                "idempotency_key is already bound to a different request in this scope"
            )
        logger.info("idempotency_hit", key_digest=_digest(key)[:12])
        return response if isinstance(response, dict) else None

    async def set(
        self, scope: str, key: str, fingerprint: str, response: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Bind (scope, key) to this request's response unless already bound.

        Returns the response that is bound once this call ends, so a caller
        never answers with a result the key does not hold: `response` when this
        call won the binding, or the earlier binding when a concurrent duplicate
        (a retry sent while the original was still running) completed first.
        Raises IdempotencyConflictError when a different request won it. None
        means the binding vanished between the two calls (expiry); the caller
        keeps its own response.
        """
        won = await self.client.set(
            self._redis_key(scope, key),
            json.dumps({"fingerprint": fingerprint, "response": response}, default=str),
            ex=self.ttl,
            nx=True,
        )
        if won:
            return response
        return await self.get(scope, key, fingerprint)

    async def close(self) -> None:
        await self.client.aclose()
