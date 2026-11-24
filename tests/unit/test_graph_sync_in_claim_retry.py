"""A transport retry inside one claim is reported, so the version counts as uncertain.

Contract: docs/contracts/data/graph_sync_intents.md rule 5 (#419 review). The
router may make two requests inside one claim; when the first one timed out,
its outcome is unknown and it may still be applied after the second is
acknowledged. ``PacketRouter.route`` reports that through ``on_retry``,
``notify_graph_sync`` through ``GraphSyncOutcome.retried``, and the
coordinator records it in the intent's ``last_error``. The tenant is synthetic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from constellation_node_sdk.gate import GateHTTPError, GateTimeoutError
from constellation_node_sdk.transport import create_transport_packet

from app.engines.domain_yaml_reader import DomainYamlReader
from app.engines.packet_router import GraphSyncOutcome, GraphSyncOutcomeKind, PacketRouter
from app.services import pg_store, side_effect_coordinator

pytestmark = pytest.mark.unit

TENANT = "synthco"
OK = {"status": "success", "entity_type": "depots", "synced_count": 1}
SPEC: dict[str, Any] = {
    "name": TENANT,
    "version": "1.0.0",
    "ontology": {
        "nodes": [
            {
                "label": "Depot",
                "properties": [
                    {"name": "depot_id", "type": "string"},
                    {"name": "capacity", "type": "float"},
                ],
            }
        ]
    },
    "sync": {
        "endpoints": [{"path": "/v1/sync/depots", "targetnode": "Depot", "idproperty": "depot_id"}]
    },
}


def _router(tmp_path: Path) -> PacketRouter:
    root = tmp_path / "domains"
    (root / TENANT).mkdir(parents=True)
    (root / TENANT / "spec.yaml").write_text(yaml.safe_dump(SPEC), encoding="utf-8")
    return PacketRouter(gate_url="https://gate-node:8080", domain_reader=DomainYamlReader(root))


def _answer(payload: dict[str, Any]) -> Any:
    return create_transport_packet(
        action="sync",
        payload=payload,
        tenant=TENANT,
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )


async def _sync(router: PacketRouter, *answers: Any) -> list[GraphSyncOutcome]:
    outcomes: list[GraphSyncOutcome] = []
    with (
        patch.object(router._client, "execute", new_callable=AsyncMock, side_effect=answers),
        patch("app.engines.packet_router.asyncio.sleep", new_callable=AsyncMock),
    ):
        await router.notify_graph_sync(
            tenant_id=TENANT,
            entity_id="d-1",
            fields={"capacity": 2.5},
            domain=TENANT,
            node_label="Depot",
            on_outcome=outcomes.append,
            operation_key="op-1",
        )
    return outcomes


@pytest.mark.asyncio
async def test_an_ack_after_a_timed_out_request_reports_the_retry(tmp_path: Path) -> None:
    (outcome,) = await _sync(_router(tmp_path), GateTimeoutError("slow"), _answer(OK))
    assert outcome.kind is GraphSyncOutcomeKind.ACKNOWLEDGED
    assert outcome.retried is True


@pytest.mark.asyncio
async def test_a_refusal_after_a_timed_out_request_reports_the_retry(tmp_path: Path) -> None:
    refused = GateHTTPError("no route", status_code=404)
    (outcome,) = await _sync(_router(tmp_path), GateTimeoutError("slow"), refused)
    assert outcome.kind is GraphSyncOutcomeKind.REFUSED
    assert outcome.retried is True


@pytest.mark.asyncio
async def test_a_single_request_reports_no_retry(tmp_path: Path) -> None:
    (outcome,) = await _sync(_router(tmp_path), _answer(OK))
    assert outcome.kind is GraphSyncOutcomeKind.ACKNOWLEDGED
    assert outcome.retried is False


@pytest.mark.asyncio
async def test_route_calls_on_retry_once_per_retry(tmp_path: Path) -> None:
    router = _router(tmp_path)
    calls: list[None] = []
    from app.engines.packet_router import NodeTarget

    with (
        patch.object(
            router._client,
            "execute",
            new_callable=AsyncMock,
            side_effect=[GateTimeoutError("slow"), _answer(OK)],
        ),
        patch("app.engines.packet_router.asyncio.sleep", new_callable=AsyncMock),
    ):
        await router.route(
            NodeTarget.GRAPH,
            "sync",
            TENANT,
            {"entity_type": "depots", "batch": []},
            idempotency_key="k",
            on_retry=lambda: calls.append(None),
        )
    assert len(calls) == 1


def test_the_coordinator_marks_with_the_store_constant() -> None:
    assert side_effect_coordinator.GRAPH_RETRIED_IN_CLAIM == pg_store.GRAPH_RETRIED_IN_CLAIM
