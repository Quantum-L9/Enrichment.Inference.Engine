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


async def handle_community_export(
    tenant: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Handle community_export packets from GRAPH node.

    These contain Louvain community labels to be injected as
    enrichment targets for re-enrichment passes.

    EIE-SEC-01: the tenant is ``packet.tenant.org_id`` — the ``tenant``
    argument the SDK runtime passes — and nothing else. A ``tenant_id`` in the
    payload is caller-asserted data and is not read; it must never redirect
    one tenant's communities into another tenant's enrichment queue.
    """
    from .graph_return_channel import GraphReturnChannel

    tenant_id = tenant
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

    EIE-SEC-01: tenant comes only from ``packet.tenant.org_id`` (the ``tenant``
    argument); a payload ``tenant_id`` is not an override.
    """
    tenant_id = tenant
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
    start_graph_intent_recovery()


#: How often a process replays the durable graph intents that are due (A2).
GRAPH_INTENT_RECOVERY_INTERVAL_SECONDS = 30.0


def start_graph_intent_recovery() -> bool:
    """Start this process's bounded graph-intent recovery poll (A2), once.

    Called from startup registration, inside the running event loop: an idle
    node that restarted with intents left `pending` replays them without
    waiting for traffic. It is an in-process task of the one coordinator --
    no queue, no daemon -- and its first pass runs one interval after start,
    by which time startup has initialised the store; a pass that finds the
    store unavailable is logged and retried on the next tick. Returns False,
    starting nothing, when no event loop is running or the poll already runs.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    from app.core.config import get_settings

    from .side_effect_coordinator import get_side_effect_coordinator

    started = get_side_effect_coordinator().start_graph_recovery(
        get_settings(), interval_seconds=GRAPH_INTENT_RECOVERY_INTERVAL_SECONDS
    )
    logger.info("graph_intent_recovery_started", started=started)
    return started
