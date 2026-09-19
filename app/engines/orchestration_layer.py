"""
app/engines/orchestration_layer.py
Orchestration Layer — wires the full L9 enrichment constellation.

Integration fixes applied (PR#21 merge pass + TASK-021):
    Side effects (persist / graph-sync / score-invalidate / event) converge through
    SideEffectCoordinator. enrich-and-sync calls handle_enrich once; duplicates removed.
"""

from __future__ import annotations

from typing import Any

import structlog
from constellation_node_sdk.runtime.handlers import register_handler

from app.core.config import get_settings
from app.engines.handlers import (
    handle_converge,
    handle_discover,
    handle_enrich,
    handle_enrichbatch,
    handle_simulate,
    handle_writeback,
    init_handlers,
)

logger = structlog.get_logger(__name__)


def register(kb, idem_store=None, domain_reader=None) -> None:
    settings = get_settings()

    init_handlers(kb=kb, idem=idem_store, domain_reader=domain_reader)

    register_handler("enrich", handle_enrich)
    register_handler("enrichbatch", handle_enrichbatch)
    register_handler("converge", handle_converge)
    register_handler("discover", handle_discover)
    register_handler("simulate", handle_simulate)
    register_handler("writeback", handle_writeback)
    register_handler("enrich-and-sync", _make_enrich_and_sync_handler(kb, idem_store))

    logger.info(
        "orchestration.registered",
        handlers=[
            "enrich",
            "enrichbatch",
            "converge",
            "discover",
            "simulate",
            "writeback",
            "enrich-and-sync",
        ],
        gate_url=settings.gate_url,
    )


def _make_enrich_and_sync_handler(kb, idem_store):
    """Composite: enrich -> persist -> graph sync -> score invalidate -> event emit."""

    async def handle_enrich_and_sync(
        tenant: str,
        payload: dict[str, Any],
        packet: Any | None = None,
    ) -> dict[str, Any]:
        enrich_result = await handle_enrich(tenant, payload)

        if enrich_result.get("state") != "completed":
            return enrich_result

        # Side effects already committed once inside handle_enrich via SideEffectCoordinator.
        # Re-entry with the same semantic key is a no-op (idempotent).
        enrich_result["side_effects"] = "coordinated"
        return enrich_result

    handle_enrich_and_sync.__name__ = "handle_enrich_and_sync"
    return handle_enrich_and_sync


# EIE-006: `run_outcome_feedback` lived here, built on a module-level
# GraphSyncClient constructed in register(). Neither had a caller: the client was
# instantiated at startup and none of its methods was reachable, while the live
# EIE -> CEG egress is app/engines/packet_router.py. Keeping a second outbound
# client wired but unused is worse than having none — CLAUDE.md listed it as part
# of the active transport bundle, so a reader tracing egress landed on the module
# that never sends anything. The construction is gone; graph_sync_client.py itself
# stays as a declared staged artifact (tests/compliance/test_module_reachability.py).
