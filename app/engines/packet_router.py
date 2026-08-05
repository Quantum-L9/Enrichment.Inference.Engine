"""
app/engines/packet_router.py

Gate-routed transport router for inter-node constellation communication.

This preserves the existing router-style API used by ENRICH while moving the
actual transport to `constellation_node_sdk.GateClient`.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Any

import structlog
from constellation_node_sdk.gate import GateClient, GateClientConfig
from constellation_node_sdk.transport import TransportPacket, create_transport_packet

logger = structlog.get_logger("packet_router")

_ROUTE_TIMEOUT = 30.0
_RETRY_ATTEMPTS = 2
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})


class NodeTarget(StrEnum):
    GRAPH = "graph"
    SCORE = "score"
    ROUTE = "route"
    SIGNAL = "signal"
    FORECAST = "forecast"
    HANDOFF = "handoff"


class NodeUnreachableError(Exception):
    """Raised when all retry attempts to a constellation node are exhausted."""


def _build_envelope(
    action: str,
    tenant_id: str,
    payload: dict[str, Any],
    correlation_id: str | None = None,
) -> TransportPacket:
    """
    Build a Gate-routed TransportPacket for inter-node dispatch.
    """
    return create_transport_packet(
        action=action,
        payload=payload,
        tenant=tenant_id,
        source_node="enrichment-engine",
        destination_node="gate",
        reply_to="enrichment-engine",
        correlation_id=correlation_id,
        classification="internal",
        compliance_tags=("INTER_NODE",),
    )


class PacketRouter:
    """
    Dispatches transport work through Gate.

    The `target` argument is retained for call-site compatibility and logging,
    but routing authority lives in Gate rather than in ENRICH peer URLs.
    """

    def __init__(self, gate_url: str, timeout: float = _ROUTE_TIMEOUT) -> None:
        self._client = GateClient(
            GateClientConfig(
                gate_url=gate_url,
                local_node="enrichment-engine",
                timeout_seconds=timeout,
            )
        )
        self._gate_url = gate_url
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def route(
        self,
        target: NodeTarget,
        action: str,
        tenant_id: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Dispatch a transport packet through Gate.
        """
        packet = _build_envelope(action, tenant_id, payload, correlation_id)

        last_exc: Exception | None = None
        for attempt in range(_RETRY_ATTEMPTS + 1):
            try:
                response = await self._client.send_to_gate(packet)
                data = dict(response.payload)
                data["packet_id"] = str(response.header.packet_id)
                logger.info(
                    "packet_routed",
                    target=target.value,
                    action=action,
                    packet_id=str(packet.header.packet_id),
                    gate_url=self._gate_url,
                )
                return data

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "packet_route_error",
                    target=target.value,
                    action=action,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < _RETRY_ATTEMPTS:
                    await asyncio.sleep(2**attempt)

        raise NodeUnreachableError(
            f"Node {target.value} unreachable after {_RETRY_ATTEMPTS + 1} attempts"
        ) from last_exc

    def route_fire_and_forget(
        self,
        target: NodeTarget,
        action: str,
        tenant_id: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
    ) -> None:
        """
        Dispatch without awaiting. Errors are logged, never raised.
        Use for non-critical downstream notifications.
        """

        async def _safe_route() -> None:
            try:
                await self.route(target, action, tenant_id, payload, correlation_id)
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
        Notify the graph node to sync enriched entity fields.

        Returns the graph response or None if graph node not configured.
        """
        try:
            return await self.route(
                NodeTarget.GRAPH,
                "graph-sync",
                tenant_id,
                {"entity_id": entity_id, "fields": fields, "domain": domain or ""},
                correlation_id,
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
    _router_singleton = PacketRouter(gate_url=settings.gate_url)
    return _router_singleton
