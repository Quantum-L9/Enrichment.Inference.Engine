"""
Graph Sync Client — Gate-only egress for graph sync/match/outcome.

All outbound inter-node work is sent to Gate using `TransportPacket`.
This client preserves the existing enrich/graph orchestration surface while
removing direct peer `/v1/execute` calls from ENRICH.
"""

from __future__ import annotations

from app.utils.safe_convert import safe_float
from typing import Any

import structlog
from constellation_node_sdk.gate import GateClient, GateClientConfig
from constellation_node_sdk.transport import TransportPacket, create_transport_packet

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
        source_node: str = "enrichment-engine",
        timeout: int = 30,
    ) -> None:
        self._client = GateClient(
            GateClientConfig(
                gate_url=gate_url,
                local_node=source_node,
                timeout_seconds=safe_float(timeout),
            )
        )
        self._source = source_node

    async def sync_entities(
        self,
        entity_type: str,
        batch: list[dict[str, Any]],
        tenant: str,
        parent_packet: TransportPacket | None = None,
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
    ) -> dict[str, Any]:
        """Send match outcome feedback via Gate."""
        packet = self._build_packet(
            action="outcome",
            payload=outcome,
            tenant=tenant,
            parent_packet=parent_packet,
            intent="outcome_feedback",
        )
        return await self._send(packet)

    def _build_packet(
        self,
        action: str,
        payload: dict[str, Any],
        tenant: str,
        parent_packet: TransportPacket | None = None,
        intent: str = "",
    ) -> TransportPacket:
        if parent_packet is not None:
            return parent_packet.derive(
                action=action,
                source_node=self._source,
                destination_node="gate",
                reply_to=self._source,
                payload={**payload, "intent": intent or action},
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
        )

    async def _send(self, packet: TransportPacket) -> dict[str, Any]:
        try:
            response = await self._client.send_to_gate(packet)
            payload = dict(response.payload)
            payload["packet_id"] = str(response.header.packet_id)
            return payload
        except Exception as exc:
            logger.error(
                "graph_sync_error",
                action=packet.header.action,
                packet_id=str(packet.header.packet_id),
                error=type(exc).__name__,
            )
            return {"status": "failed", "error": "connection_failed"}
