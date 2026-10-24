"""
Unit tests for orchestration_layer.register() and SDK runtime execution.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from constellation_node_sdk.runtime.execution import execute_transport_packet
from constellation_node_sdk.runtime.handlers import (
    clear_handlers,
    register_handler,
    registered_actions,
)
from constellation_node_sdk.transport import create_transport_packet


@pytest.fixture(autouse=True)
def reset_registry():
    """Ensure clean registry for each test."""
    clear_handlers()
    yield
    clear_handlers()


def test_register_wires_expected_actions():
    from app.engines import orchestration_layer

    kb = MagicMock()
    kb.index.files_loaded = []
    kb.index.polymers = []
    kb.index.total_grades = 0
    kb.index.total_rules = 0

    # EIE-006: no GraphSyncClient patch — register() no longer constructs one.
    # It built an outbound client at startup whose methods nothing ever called.
    with patch.object(orchestration_layer, "init_handlers"):
        orchestration_layer.register(kb=kb, idem_store=None)

    handlers = registered_actions()
    for action in ["enrich", "enrichbatch", "converge", "discover", "enrich-and-sync"]:
        assert action in handlers, f"Action '{action}' not registered"


def test_register_constructs_no_second_outbound_client():
    """EIE-006: PacketRouter is the live EIE -> CEG egress; there is not a second.

    A client that is constructed but unreachable is worse than absent — CLAUDE.md
    named it in the active transport bundle, sending readers to a module that
    never sends anything.
    """
    from app.engines import orchestration_layer

    assert not hasattr(orchestration_layer, "GraphSyncClient")
    assert not hasattr(orchestration_layer, "_graph_client")
    assert not hasattr(orchestration_layer, "run_outcome_feedback")


@pytest.mark.asyncio
async def test_sdk_runtime_dispatches_to_enrich():
    mock_handler = AsyncMock(return_value={"state": "completed", "fields": {"grade": "A"}})
    register_handler("enrich", mock_handler)

    packet = create_transport_packet(
        action="enrich",
        payload={"entity_id": "e-001"},
        tenant="test-tenant",
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )
    result = await execute_transport_packet(
        packet,
        node_name="enrichment-engine",
        dev_mode=True,
        allowed_actions=("enrich",),
    )
    assert result.header.packet_type == "response"
    assert result.payload["state"] == "completed"
    mock_handler.assert_awaited_once_with("test-tenant", {"entity_id": "e-001"})


@pytest.mark.asyncio
async def test_sdk_runtime_raises_for_unknown_action():
    packet = create_transport_packet(
        action="unknown-action",
        payload={},
        tenant="test",
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )
    with pytest.raises(ValueError, match="unknown-action"):
        await execute_transport_packet(
            packet,
            node_name="enrichment-engine",
            dev_mode=True,
        )


@pytest.mark.asyncio
async def test_transport_packet_requires_action():
    with pytest.raises(ValueError, match="action"):
        create_transport_packet(
            action="",
            payload={},
            tenant="test",
            source_node="gate",
            destination_node="enrichment-engine",
            reply_to="gate",
        )


@pytest.mark.asyncio
async def test_startup_wires_domain_reader_into_converge():
    """Startup gives the converge handlers a domain reader.

    Without one, `_load_domain_convergence_context` returned no rules for every
    request, so the domain's inference rules never ran on the live path.
    """
    from app import main
    from app.engines import handlers
    from app.engines.domain_yaml_reader import DomainYamlReader

    with (
        patch.object(main, "setup_logging"),
        patch.object(main, "KBResolver"),
        patch.object(main, "IdempotencyStore", side_effect=RuntimeError("no redis")),
        patch("app.services.chassis_handlers.register_all_handlers"),
        patch("app.services.pg_store.init_engine"),
        patch("app.services.event_emitter.get_emitter"),
        patch("app.api.v1.converge.configure"),
        patch.object(main, "_register_with_gate", AsyncMock(return_value=None)),
        patch.object(main, "start_reregistration_loop", return_value=None),
        patch.object(main, "_gate_registered", None),
        # Startup records the registry (and hydrate) outcome module-wide; restore
        # it so readiness tests that run later do not inherit this process state.
        patch.object(main, "_domain_registry_state", "not_attempted"),
        patch.object(handlers, "_domain_reader", None),
    ):
        await main.EnrichmentLifecycle().startup()
        reader = handlers._domain_reader

    assert isinstance(reader, DomainYamlReader)
    assert reader.load("plasticos").raw_spec["inference_rules"]


@pytest.mark.parametrize(
    "domain_id",
    ["../plasticos", "plasticos/../plasticos", "Res.Partner", "res_partner", "systemdb"],
)
def test_domain_reader_refuses_ids_outside_its_root(domain_id):
    """Payload-supplied ids reach the filesystem once the reader is wired.

    The reader applies the identity grammar (tenant id == spec id == Neo4j
    database name): lowercase, `^[a-z][a-z0-9.-]{2,62}$`, no `system` prefix.
    A dotted id such as `res.partner` is legal under it and resolves to
    `domains/res.partner/spec.yaml` (absent -> FileNotFoundError), so the
    refused cases here are path escapes and grammar violations only.
    """
    from pathlib import Path

    from app.engines.domain_yaml_reader import DomainYamlReader

    reader = DomainYamlReader(Path(__file__).resolve().parents[2] / "domains")
    with pytest.raises(ValueError, match="Invalid domain id"):
        reader.load(domain_id)
