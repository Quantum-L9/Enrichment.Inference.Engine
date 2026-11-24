"""Rule 6: the row carries the intent's version when the endpoint declares ``sync_version``.

Contract: docs/contracts/data/graph_sync_intents.md rule 6 (CTO ruling on the
#419 late-write thread). The tenant is synthetic.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from constellation_node_sdk.gate import GateTimeoutError
from constellation_node_sdk.transport import create_transport_packet

from app.engines import sync_field_set as fs
from app.engines.domain_yaml_reader import DomainYamlReader
from app.engines.packet_router import GraphSyncOutcome, GraphSyncOutcomeKind, PacketRouter

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
                    {"name": "sync_version", "type": "int"},
                ],
            }
        ]
    },
    "sync": {
        "endpoints": [{"path": "/v1/sync/depots", "targetnode": "Depot", "idproperty": "depot_id"}]
    },
}


def _legacy_spec() -> dict[str, Any]:
    spec = copy.deepcopy(SPEC)
    node = spec["ontology"]["nodes"][0]
    node["properties"] = [p for p in node["properties"] if p["name"] != "sync_version"]
    return spec


def _router(tmp_path: Path, spec: dict[str, Any] = SPEC) -> PacketRouter:
    root = tmp_path / "domains"
    (root / TENANT).mkdir(parents=True)
    (root / TENANT / "spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
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


async def _sync(
    router: PacketRouter, *answers: Any, fields: dict[str, Any], version: int | None
) -> tuple[AsyncMock, list[GraphSyncOutcome]]:
    outcomes: list[GraphSyncOutcome] = []
    execute = AsyncMock(side_effect=answers)
    with (
        patch.object(router._client, "execute", execute),
        patch("app.engines.packet_router.asyncio.sleep", new_callable=AsyncMock),
    ):
        await router.notify_graph_sync(
            tenant_id=TENANT,
            entity_id="d-1",
            fields=fields,
            domain=TENANT,
            node_label="Depot",
            on_outcome=outcomes.append,
            operation_key="op-1",
            sync_version=version,
        )
    return execute, outcomes


def _rows(execute: AsyncMock) -> list[dict[str, Any]]:
    return [call.kwargs["payload"]["batch"][0] for call in execute.await_args_list]


@pytest.mark.asyncio
async def test_a_declared_endpoint_receives_the_version_on_the_row(tmp_path: Path) -> None:
    execute, outcomes = await _sync(
        _router(tmp_path), _answer(OK), fields={"capacity": 2.5}, version=41
    )
    assert _rows(execute) == [{"depot_id": "d-1", "capacity": 2.5, "sync_version": 41}]
    assert outcomes[0].kind is GraphSyncOutcomeKind.ACKNOWLEDGED


@pytest.mark.asyncio
async def test_a_value_of_that_name_is_replaced_by_the_version(tmp_path: Path) -> None:
    execute, _ = await _sync(
        _router(tmp_path), _answer(OK), fields={"capacity": 2.5, "sync_version": 999}, version=7
    )
    assert _rows(execute)[0]["sync_version"] == 7


@pytest.mark.asyncio
async def test_every_request_of_one_version_carries_the_same_number(tmp_path: Path) -> None:
    execute, outcomes = await _sync(
        _router(tmp_path),
        GateTimeoutError("slow"),
        _answer(OK),
        fields={"capacity": 2.5},
        version=12,
    )
    first, second = execute.await_args_list
    assert first.kwargs["payload"] == second.kwargs["payload"]
    assert first.kwargs["idempotency_key"] == second.kwargs["idempotency_key"]
    assert _rows(execute)[0]["sync_version"] == 12
    assert outcomes[0].retried is True


@pytest.mark.asyncio
async def test_without_a_durable_version_nothing_of_that_name_is_sent(tmp_path: Path) -> None:
    execute, _ = await _sync(
        _router(tmp_path), _answer(OK), fields={"capacity": 2.5, "sync_version": 3}, version=None
    )
    assert "sync_version" not in _rows(execute)[0]


@pytest.mark.asyncio
async def test_an_undeclared_endpoint_is_legacy_and_receives_no_version(tmp_path: Path) -> None:
    execute, _ = await _sync(
        _router(tmp_path, _legacy_spec()), _answer(OK), fields={"capacity": 2.5}, version=41
    )
    assert _rows(execute) == [{"depot_id": "d-1", "capacity": 2.5}]


def test_the_version_is_never_selected_as_a_value() -> None:
    schema = {
        "depot_id": {"name": "depot_id", "type": "string"},
        "capacity": {"name": "capacity", "type": "float"},
        "sync_version": {"name": "sync_version", "type": "int"},
    }
    selection = fs.select(
        admitted={"capacity": 2.5, "sync_version": 5},
        observed={"sync_version": 6},
        graph_origin=(),
        row_schema=schema,
        id_property="depot_id",
    )
    assert selection.values == {"capacity": 2.5}
