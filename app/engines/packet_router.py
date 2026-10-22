"""
app/engines/packet_router.py

Gate-routed transport router for inter-node constellation communication.

Every packet leaves through the one EIE Gate client
(``app.services.gate_client``) and is addressed to Gate; Gate resolves the
destination by action. EIE never names a peer.

Seam contract (seam audit 2026-09-02):

* ``notify_graph_sync`` speaks Cognitive.Engine.Graphs' executable ``sync``
  contract -- ``{"entity_type": <sync endpoint>, "batch": [row, ...]}`` where
  each row carries the endpoint's id property -- instead of the retired
  ``graph-sync`` dialect that no node owned and no Gate route resolved.
* Retry ownership for EIE-originated side effects lives HERE, once. Gate
  attempts every seam action exactly once (Gate replay_safety), so a retry
  below is the only retry. A retry is only taken when it cannot multiply a
  domain effect: Gate unreachable (nothing executed), or a timeout / 5xx on a
  packet that carries a stable idempotency key (Gate answers the duplicate from
  its keyed cache once the first attempt completed).
* One deadline per attempt; an operation is at most ``_MAX_ATTEMPTS`` attempts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from enum import StrEnum
from typing import Any

import structlog
from constellation_node_sdk.gate import (
    GateClientError,
    GateConnectionError,
    GateHTTPError,
    GateTimeoutError,
)

from app.services.gate_client import EIE_NODE_NAME, build_gate_client

logger = structlog.get_logger("packet_router")

# Per-attempt operation budget. Graph sync runs inside EIE's own inbound budget
# when triggered by a Gate-dispatched `enrich`, so it must stay well under the
# node cap EIE advertises (25 s): two attempts plus backoff fit inside it.
_ROUTE_TIMEOUT = 10.0
_MAX_ATTEMPTS = 2
_BACKOFF_BASE_SECONDS = 1.0

# CEG sync contract defaults for the plasticos domain
# (Cognitive.Engine.Graphs/domains/plasticos/spec.yaml `sync.endpoints`).
DEFAULT_GRAPH_SYNC_ENTITY_TYPE = "facilities"
DEFAULT_GRAPH_SYNC_ID_PROPERTY = "facility_id"

_PRIMITIVE = (str, int, float, bool, type(None))


class NodeTarget(StrEnum):
    """Logical target names kept for call-site readability and logs only.

    Routing authority is Gate's: the *action* selects the destination. A target
    here never becomes a URL.
    """

    GRAPH = "graph"
    SCORE = "score"
    ROUTE = "route"
    SIGNAL = "signal"
    FORECAST = "forecast"
    HANDOFF = "handoff"


class NodeUnreachableError(Exception):
    """Raised when the Gate-routed operation could not be completed."""


def _retry_allowed(exc: BaseException, *, has_idempotency_key: bool) -> bool:
    """Whether a second attempt cannot duplicate a domain effect.

    * Gate unreachable: nothing was executed anywhere -> safe.
    * Timeout or Gate 5xx: the worker may have executed -> safe only under a
      stable idempotency key, which Gate answers from its keyed cache.
    * Any 4xx (unknown route, rejected packet, policy): never -- the same packet
      would be rejected the same way.
    """
    if isinstance(exc, GateConnectionError):
        return True
    if isinstance(exc, GateTimeoutError):
        return has_idempotency_key
    if isinstance(exc, GateHTTPError):
        return exc.is_server_error and has_idempotency_key
    return False


def _graph_sync_row(
    *,
    entity_id: str,
    fields: dict[str, Any],
    domain: str | None,
    id_property: str,
) -> dict[str, Any]:
    """Project enriched fields onto one CEG sync row.

    CEG applies ``SET n += row`` in Cypher, so every value must be a Neo4j
    property primitive. Nested structures are carried as JSON strings rather
    than dropped, so nothing the enrichment produced is silently lost.
    """
    row: dict[str, Any] = {id_property: entity_id, "entity_id": entity_id}
    if domain:
        row["domain"] = domain
    for key, value in fields.items():
        if key in {id_property, "entity_id"}:
            continue
        if (
            isinstance(value, _PRIMITIVE)
            or isinstance(value, list)
            and all(isinstance(v, _PRIMITIVE) for v in value)
        ):
            row[key] = value
        else:
            row[key] = json.dumps(value, sort_keys=True, default=str)
    row["enriched_by"] = EIE_NODE_NAME
    return row


def _sync_idempotency_key(tenant_id: str, entity_id: str, row: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(row, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"eie:sync:{tenant_id}:{entity_id}:{digest}"


class PacketRouter:
    """
    Dispatches transport work through Gate.

    The `target` argument is retained for call-site compatibility and logging,
    but routing authority lives in Gate rather than in ENRICH peer URLs.
    """

    def __init__(
        self,
        gate_url: str,
        timeout: float = _ROUTE_TIMEOUT,
        *,
        graph_sync_entity_type: str = DEFAULT_GRAPH_SYNC_ENTITY_TYPE,
        graph_sync_id_property: str = DEFAULT_GRAPH_SYNC_ID_PROPERTY,
    ) -> None:
        self._client = build_gate_client(gate_url, timeout_seconds=timeout)
        self._gate_url = self._client.gate_url
        self._timeout_ms = int(timeout * 1000)
        self._graph_sync_entity_type = graph_sync_entity_type
        self._graph_sync_id_property = graph_sync_id_property
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def route(
        self,
        target: NodeTarget,
        action: str,
        tenant_id: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """
        Dispatch one logical operation through Gate.

        The SDK builds the root packet (destination Gate, EIE identity, one
        deadline, signing); this layer owns only the domain payload, the
        logical-operation identity, and the bounded retry policy above.
        """
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._client.execute(
                    action=action,
                    payload=payload,
                    tenant=tenant_id,
                    idempotency_key=idempotency_key,
                    timeout_ms=self._timeout_ms,
                    correlation_id=correlation_id,
                    compliance_tags=("INTER_NODE",),
                )
                data = dict(response.payload)
                data["packet_id"] = str(response.header.packet_id)
                logger.info(
                    "packet_routed",
                    target=target.value,
                    action=action,
                    packet_id=str(response.header.packet_id),
                    packet_type=response.header.packet_type,
                    gate_url=self._gate_url,
                    attempt=attempt,
                )
                return data

            except GateClientError as exc:
                last_exc = exc
                retry = attempt < _MAX_ATTEMPTS and _retry_allowed(
                    exc, has_idempotency_key=bool(idempotency_key)
                )
                logger.warning(
                    "packet_route_error",
                    target=target.value,
                    action=action,
                    attempt=attempt,
                    error=type(exc).__name__,
                    detail=str(exc),
                    will_retry=retry,
                )
                if not retry:
                    break
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

        raise NodeUnreachableError(
            f"Gate-routed {action!r} for {target.value} failed: "
            f"{type(last_exc).__name__ if last_exc else 'unknown'}"
        ) from last_exc

    def route_fire_and_forget(
        self,
        target: NodeTarget,
        action: str,
        tenant_id: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        """
        Dispatch without awaiting. Errors are logged, never raised.
        Use for non-critical downstream notifications.
        """

        async def _safe_route() -> None:
            try:
                await self.route(
                    target,
                    action,
                    tenant_id,
                    payload,
                    correlation_id,
                    idempotency_key=idempotency_key,
                )
            except Exception as exc:
                logger.warning(
                    "fire_and_forget_failed",
                    target=target.value,
                    action=action,
                    error=str(exc),
                )

        task = asyncio.create_task(_safe_route())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def notify_graph_sync(
        self,
        tenant_id: str,
        entity_id: str,
        fields: dict[str, Any],
        domain: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Sync enriched entity fields into the graph through Gate (action `sync`,
        owned by Cognitive.Engine.Graphs).

        Returns CEG's sync response payload, or None when the operation could
        not be completed. There is no fallback path: a Gate outage or an
        unresolvable route is a logged, bounded failure.
        """
        row = _graph_sync_row(
            entity_id=entity_id,
            fields=fields,
            domain=domain,
            id_property=self._graph_sync_id_property,
        )
        try:
            return await self.route(
                NodeTarget.GRAPH,
                "sync",
                tenant_id,
                {"entity_type": self._graph_sync_entity_type, "batch": [row]},
                correlation_id,
                idempotency_key=_sync_idempotency_key(tenant_id, entity_id, row),
            )
        except NodeUnreachableError as exc:
            logger.warning("graph_sync_failed", entity_id=entity_id, error=str(exc))
            return None

    async def notify_score_invalidate(
        self,
        tenant_id: str,
        entity_id: str,
        domain: str | None = None,
    ) -> None:
        """Fire-and-forget score invalidation after entity enrichment."""
        self.route_fire_and_forget(
            NodeTarget.SCORE,
            "score-invalidate",
            tenant_id,
            {"entity_id": entity_id, "domain": domain or ""},
        )

    async def close(self) -> None:
        """No-op close; GateClient is request-scoped."""
        return None


_router_singleton: PacketRouter | None = None


def get_router(settings: Any) -> PacketRouter:
    """Module-level singleton router. One instance per process."""
    global _router_singleton
    if _router_singleton is not None:
        return _router_singleton
    _router_singleton = PacketRouter(
        gate_url=settings.gate_url,
        graph_sync_entity_type=getattr(
            settings, "graph_sync_entity_type", DEFAULT_GRAPH_SYNC_ENTITY_TYPE
        ),
        graph_sync_id_property=getattr(
            settings, "graph_sync_id_property", DEFAULT_GRAPH_SYNC_ID_PROPERTY
        ),
    )
    return _router_singleton
