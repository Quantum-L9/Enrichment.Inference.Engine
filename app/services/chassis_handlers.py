"""
Supplemental SDK handler registration for ENRICH-specific transport actions.

This file preserves the older import path while delegating registration to the
SDK runtime registry and canonical handler implementations in
`app.services.graph_return_channel`.
"""

from __future__ import annotations

from typing import Any

import structlog
from constellation_node_sdk.runtime.handlers import register_handler, registered_actions

from .graph_return_channel import (
    build_graph_inference_result_envelope,
    handle_graph_inference_result,
)

logger = structlog.get_logger(__name__)


def _authoritative_tenant(tenant: str, payload: dict[str, Any]) -> str | dict[str, Any]:
    """Transport tenant wins. Matching payload tenant_id is accepted; mismatch is rejected."""
    claimed = payload.get("tenant_id")
    if claimed in (None, ""):
        return tenant
    if claimed == tenant:
        return tenant
    return {"status": "rejected", "error": "tenant_mismatch", "tenant_id": tenant}


async def handle_community_export(
    tenant: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Handle community_export packets from GRAPH node.

    These contain Louvain community labels to be injected as
    enrichment targets for re-enrichment passes.
    """
    from .graph_return_channel import GraphReturnChannel

    resolved = _authoritative_tenant(tenant, payload)
    if isinstance(resolved, dict):
        return resolved
    tenant_id = resolved
    communities = payload.get("communities", [])

    if not communities:
        return {"status": "ok", "targets_queued": 0}

    # Convert community labels to inference outputs
    inference_outputs = [
        {
            "entity_id": c.get("entity_id"),
            "field": "community_id",
            "value": c.get("community_id"),
            "confidence": 0.95,  # Louvain is deterministic
            "rule": "louvain_community_detection",
        }
        for c in communities
        if c.get("entity_id") and c.get("community_id") is not None
    ]

    if not inference_outputs:
        return {"status": "ok", "targets_queued": 0}

    packet = build_graph_inference_result_envelope(
        tenant_id=tenant_id,
        inference_outputs=inference_outputs,
    )

    channel = GraphReturnChannel.get_instance()
    count = await channel.submit(packet)

    return {
        "status": "accepted",
        "targets_queued": count,
        "packet_id": str(packet.header.packet_id),
    }


async def handle_schema_proposal(
    tenant: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Handle schema_proposal packets.

    These contain proposed field additions from schema discovery
    that should be reviewed and potentially added to the domain schema.
    """
    resolved = _authoritative_tenant(tenant, payload)
    if isinstance(resolved, dict):
        return resolved
    tenant_id = resolved
    proposed_fields = payload.get("proposed_fields", [])

    logger.info(
        "schema_proposal.received",
        tenant_id=tenant_id,
        field_count=len(proposed_fields),
    )

    # In production, this would queue for review or auto-apply
    # For now, just acknowledge receipt
    return {
        "status": "received",
        "tenant_id": tenant_id,
        "field_count": len(proposed_fields),
        "packet_id": payload.get("packet_id"),
    }


def register_all_handlers() -> None:
    """
    Register all supplemental ENRICH handlers with the SDK runtime.
    Call this at application startup.
    """
    register_handler("graph-inference-result", handle_graph_inference_result)
    register_handler("community-export", handle_community_export)
    register_handler("schema-proposal", handle_schema_proposal)

    logger.info(
        "sdk_handlers_registered",
        handlers=registered_actions(),
    )
