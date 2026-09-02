"""
Graph Sync Client — Gate-only egress for graph sync/match/outcomes.

All outbound inter-node work is sent to Gate using `TransportPacket`.
This client preserves the existing enrich/graph orchestration surface while
removing direct peer `/v1/execute` calls from ENRICH.

Actions are Cognitive.Engine.Graphs' executable handler names (`sync`,
`match`, `outcomes`) — the same names Gate's canonical ownership map assigns
to `ceg`. (`outcome`, singular, was never a CEG handler and never a Gate route;
seam audit 2026-09-02.)
"""

from __future__ import annotations

from typing import Any

import structlog
from constellation_node_sdk.gate import (
    GateClientError,
    GateConnectionError,
    GateHTTPError,
    GateSecurityError,
    GateTimeoutError,
)
from constellation_node_sdk.transport import TransportPacket, create_transport_packet

from app.services.gate_client import EIE_NODE_NAME, build_gate_client
from app.utils.safe_convert import safe_float

logger = structlog.get_logger("graph_sync_client")


class GraphSyncClient:
    """
    Gate-only client for Graph Intelligence actions.

    The public methods intentionally keep the pre-SDK interface so the
    orchestration layer can migrate transport without rewriting business logic.
    """

    def __init__(
        self,
        gate_url: str,
        source_node: str = EIE_NODE_NAME,
        timeout: int = 30,
    ) -> None:
        self._client = build_gate_client(gate_url, timeout_seconds=safe_float(timeout))
        self._source = EIE_NODE_NAME if not source_node else source_node.strip().lower()
        self._timeout_ms = int(safe_float(timeout) * 1000)

    async def sync_entities(
        self,
        entity_type: str,
        batch: list[dict[str, Any]],
        tenant: str,
        parent_packet: TransportPacket | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Sync enriched entities to the graph via Gate."""
        packet = self._build_packet(
            action="sync",
            payload={
                "entity_type": entity_type,
                "batch": batch,
            },
            tenant=tenant,
            parent_packet=parent_packet,
            intent=f"sync_{entity_type}",
            idempotency_key=idempotency_key,
        )
        return await self._send(packet)

    async def match(
        self,
        query: dict[str, Any],
        match_direction: str,
        tenant: str,
        parent_packet: TransportPacket | None = None,
        top_n: int = 10,
    ) -> dict[str, Any]:
        """Run a match query against the graph via Gate."""
        packet = self._build_packet(
            action="match",
            payload={
                "query": query,
                "match_direction": match_direction,
                "top_n": top_n,
            },
            tenant=tenant,
            parent_packet=parent_packet,
            intent=f"match_{match_direction}",
        )
        return await self._send(packet)

    async def send_outcome(
        self,
        outcome: dict[str, Any],
        tenant: str,
        parent_packet: TransportPacket | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Send match outcome feedback via Gate (CEG action `outcomes`)."""
        packet = self._build_packet(
            action="outcomes",
            payload=outcome,
            tenant=tenant,
            parent_packet=parent_packet,
            intent="outcome_feedback",
            idempotency_key=idempotency_key,
        )
        return await self._send(packet)

    def _build_packet(
        self,
        action: str,
        payload: dict[str, Any],
        tenant: str,
        parent_packet: TransportPacket | None = None,
        intent: str = "",
        idempotency_key: str | None = None,
    ) -> TransportPacket:
        if parent_packet is not None:
            return parent_packet.derive(
                action=action,
                source_node=self._source,
                destination_node="gate",
                reply_to=self._source,
                payload={**payload, "intent": intent or action},
                timeout_ms=self._timeout_ms,
            )

        return create_transport_packet(
            action=action,
            payload={**payload, "intent": intent or action},
            tenant=tenant,
            source_node=self._source,
            destination_node="gate",
            reply_to=self._source,
            classification="internal",
            compliance_tags=("GRAPH",),
            timeout_ms=self._timeout_ms,
            idempotency_key=idempotency_key,
        )

    async def _send(self, packet: TransportPacket) -> dict[str, Any]:
        """One Gate attempt; every failure is a typed, fail-closed result.

        There is no fallback transport. A Gate outage, an unroutable action, a
        rejected packet, or an untrusted response each yield a `failed` result
        with a stable `error` code the caller can act on.
        """
        try:
            response = await self._client.send_to_gate(packet)
        except GateConnectionError as exc:
            return self._failure(packet, exc, "gate_unreachable")
        except GateTimeoutError as exc:
            return self._failure(packet, exc, "gate_timeout")
        except GateHTTPError as exc:
            code = "route_unavailable" if exc.status_code == 404 else f"gate_http_{exc.status_code}"
            return self._failure(packet, exc, code)
        except GateSecurityError as exc:
            return self._failure(packet, exc, f"gate_security_{exc.direction}")
        except GateClientError as exc:
            return self._failure(packet, exc, "gate_client_error")

        payload = dict(response.payload)
        payload["packet_id"] = str(response.header.packet_id)
        payload["packet_type"] = response.header.packet_type
        return payload

    @staticmethod
    def _failure(packet: TransportPacket, exc: Exception, code: str) -> dict[str, Any]:
        logger.error(
            "graph_sync_error",
            action=packet.header.action,
            packet_id=str(packet.header.packet_id),
            error=type(exc).__name__,
            code=code,
        )
        return {"status": "failed", "error": code, "error_type": type(exc).__name__}
