# --- L9_META ---
# l9_schema: 1
# origin: l9-enrich-node
# engine: enrich
# layer: [services, integration]
# tags: [L9_INTEGRATION, graph-sync, score-invalidation]
# owner: platform
# status: active
# --- /L9_META ---
"""
app/services/graph_sync_hooks.py

GAP #22: Graph sync + score invalidation hooks.

When an enrichment pass converges:
  1. trigger_graph_sync() → send graph-sync TransportPacket to Gate
  2. invalidate_score_cache() → send score-invalidate TransportPacket to Gate

Both calls preserve lineage hints in the payload for traceability and route
through Gate instead of direct peer HTTP.

Configuration:
  - GATE_URL / settings.gate_url (optional) — if unset, Gate-routed follow-up work is skipped

Architecture compliance:
  - Gate-only egress
  - No retries (let downstream services handle idempotency)
  - Lineage forwarding mandatory in payload
  - Timeout: 10s per call
"""

from __future__ import annotations

import logging
from typing import Any

from constellation_node_sdk.gate import GateClient, GateClientConfig
from constellation_node_sdk.transport import create_transport_packet

from app.core.config import get_settings

logger = logging.getLogger(__name__)


async def trigger_graph_sync(
    entity_id: str,
    tenant_id: str,
    enriched_fields: dict[str, Any],
    confidence: float,
    lineage_id: str,
    packet_id: str,
) -> None:
    """
    Notify GRAPH via Gate of new enrichment data for entity.
    """
    settings = get_settings()
    if not settings.gate_url:
        logger.debug("graph_sync_skipped_no_gate", extra={"entity_id": entity_id})
        return

    packet = create_transport_packet(
        action="graph-sync",
        payload={
            "entity_id": entity_id,
            "tenant_id": tenant_id,
            "enriched_fields": enriched_fields,
            "confidence": confidence,
            "lineage_id": lineage_id,
            "packet_id": packet_id,
        },
        tenant=tenant_id,
        source_node="enrichment-engine",
        destination_node="gate",
        reply_to="enrichment-engine",
        classification="internal",
        compliance_tags=("GRAPH_SYNC",),
    )

    try:
        client = GateClient(
            GateClientConfig(
                gate_url=settings.gate_url,
                local_node="enrichment-engine",
                timeout_seconds=10.0,
            )
        )
        response = await client.send_to_gate(packet)
        logger.info(
            "graph_sync_triggered",
            extra={"entity_id": entity_id, "packet_id": str(response.header.packet_id)},
        )
    except Exception as exc:
        logger.exception(
            "graph_sync_failed",
            extra={"entity_id": entity_id, "gate_url": settings.gate_url, "error": str(exc)},
        )


async def invalidate_score_cache(
    entity_id: str,
    tenant_id: str,
    lineage_id: str,
    packet_id: str,
) -> None:
    """
    Notify SCORE via Gate to invalidate cached scores for entity.
    """
    settings = get_settings()
    if not settings.gate_url:
        logger.debug("score_invalidation_skipped_no_gate", extra={"entity_id": entity_id})
        return

    packet = create_transport_packet(
        action="score-invalidate",
        payload={
            "entity_id": entity_id,
            "tenant_id": tenant_id,
            "lineage_id": lineage_id,
            "packet_id": packet_id,
        },
        tenant=tenant_id,
        source_node="enrichment-engine",
        destination_node="gate",
        reply_to="enrichment-engine",
        classification="internal",
        compliance_tags=("SCORE_INVALIDATION",),
    )

    try:
        client = GateClient(
            GateClientConfig(
                gate_url=settings.gate_url,
                local_node="enrichment-engine",
                timeout_seconds=10.0,
            )
        )
        response = await client.send_to_gate(packet)
        logger.info(
            "score_cache_invalidated",
            extra={"entity_id": entity_id, "packet_id": str(response.header.packet_id)},
        )
    except Exception as exc:
        logger.exception(
            "score_invalidation_failed",
            extra={"entity_id": entity_id, "gate_url": settings.gate_url, "error": str(exc)},
        )
