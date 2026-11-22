"""A2: every entity ``sync`` attempt is classified refused / retryable / acknowledged.

The durable graph leg (side_effect_coordinator + pg_store graph intents) needs
to tell an input that can never succeed (terminal) from one that may succeed on
replay. ``notify_graph_sync`` keeps its answer (the ack, or None) and reports
the classification through ``on_outcome``. A replay of the same pinned input
sends the same row under the same idempotency key, so a replay after Gate's
cache TTL lands as a CEG ``MERGE`` on the same id, never as a second entity.

The tenant here is synthetic (``synthco``): nothing in the transport is
specific to a shipped cartridge (P05).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from constellation_node_sdk.gate import GateConnectionError, GateHTTPError, GateTimeoutError
from constellation_node_sdk.transport import create_transport_packet

from app.engines.domain_yaml_reader import DomainYamlReader
from app.engines.packet_router import (
    GraphSyncOutcome,
    GraphSyncOutcomeKind,
    GraphSyncPin,
    PacketRouter,
    graph_sync_contract_digest,
)

GATE = "https://gate-node:8080"
TENANT = "synthco"

SYNTH_SPEC: dict[str, Any] = {
    "name": TENANT,
    "version": "1.0.0",
    "ontology": {
        "nodes": [
            {
                "label": "Depot",
                "properties": [
                    {"name": "depot_id", "type": "string"},
                    {"name": "capacity", "type": "float"},
                    {
                        "name": "handled_items",
                        "type": "list",
                        "items": {
                            "type": "map",
                            "properties": [
                                {"name": "code", "type": "string", "required": True},
                                {"name": "form", "type": "string"},
                            ],
                        },
                    },
                ],
            }
        ]
    },
    "sync": {
        "endpoints": [{"path": "/v1/sync/depots", "targetnode": "Depot", "idproperty": "depot_id"}]
    },
}


def _router(tmp_path: Path, spec: dict[str, Any] = SYNTH_SPEC) -> PacketRouter:
    root = tmp_path / "domains"
    (root / TENANT).mkdir(parents=True, exist_ok=True)
    (root / TENANT / "spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    return PacketRouter(gate_url=GATE, domain_reader=DomainYamlReader(root))


def _answer(payload: dict[str, Any], *, packet_type: str | None = None) -> Any:
    packet = create_transport_packet(
        action="sync",
        payload=payload,
        tenant=TENANT,
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
    )
    if packet_type is not None:
        header = packet.header.model_copy(update={"packet_type": packet_type})
        packet = packet.model_copy(update={"header": header})
    return packet


OK = {"status": "success", "entity_type": "depots", "synced_count": 1}


async def _sync(
    router: PacketRouter,
    *,
    fields: dict[str, Any] | None = None,
    expected_contract_digest: str | None = None,
) -> tuple[dict[str, Any] | None, list[GraphSyncOutcome]]:
    outcomes: list[GraphSyncOutcome] = []
    ack = await router.notify_graph_sync(
        tenant_id=TENANT,
        entity_id="d-1",
        fields=fields if fields is not None else {"capacity": 2.5},
        domain=TENANT,
        node_label="Depot",
        expected_contract_digest=expected_contract_digest,
        on_outcome=outcomes.append,
    )
    return ack, outcomes


@pytest.mark.asyncio
async def test_acknowledged_sync_reports_acknowledged_with_the_ack(tmp_path: Path) -> None:
    router = _router(tmp_path)
    with patch.object(router._client, "execute", new_callable=AsyncMock, return_value=_answer(OK)):
        ack, outcomes = await _sync(router)
    assert ack is not None and ack["synced_count"] == 1
    assert [o.kind for o in outcomes] == [GraphSyncOutcomeKind.ACKNOWLEDGED]
    assert outcomes[0].ack == ack


@pytest.mark.asyncio
async def test_no_contract_is_refused_and_sends_nothing(tmp_path: Path) -> None:
    router = _router(tmp_path, {"name": TENANT, "ontology": {"nodes": [{"label": "Depot"}]}})
    with patch.object(router._client, "execute", new_callable=AsyncMock) as execute:
        ack, outcomes = await _sync(router)
    assert ack is None
    execute.assert_not_awaited()
    assert (outcomes[0].kind, outcomes[0].reason) == (GraphSyncOutcomeKind.REFUSED, "no_contract")


@pytest.mark.asyncio
async def test_a_changed_contract_refuses_the_pinned_input_and_sends_nothing(
    tmp_path: Path,
) -> None:
    """Changed published spec: fields pinned against the old contract are never sent."""
    router = _router(tmp_path)
    with patch.object(router._client, "execute", new_callable=AsyncMock) as execute:
        ack, outcomes = await _sync(router, expected_contract_digest="0" * 64)
    assert ack is None
    execute.assert_not_awaited()
    assert (outcomes[0].kind, outcomes[0].reason) == (
        GraphSyncOutcomeKind.REFUSED,
        "contract_changed",
    )


@pytest.mark.asyncio
async def test_the_unchanged_pinned_contract_is_sent(tmp_path: Path) -> None:
    router = _router(tmp_path)
    pin = router.graph_sync_pin(TENANT, node_label="Depot")
    assert isinstance(pin, GraphSyncPin)
    contract = router.graph_sync_contract(TENANT, node_label="Depot")
    assert contract is not None
    assert pin.entity_type == "depots"
    assert pin.contract_digest == graph_sync_contract_digest(contract)
    with patch.object(router._client, "execute", new_callable=AsyncMock, return_value=_answer(OK)):
        ack, outcomes = await _sync(router, expected_contract_digest=pin.contract_digest)
    assert ack is not None
    assert outcomes[0].kind is GraphSyncOutcomeKind.ACKNOWLEDGED


@pytest.mark.asyncio
async def test_a_rejected_typed_row_is_refused_before_anything_is_sent(tmp_path: Path) -> None:
    """P17 (EIE side): a value off its declared type refuses the row; no partial write."""
    router = _router(tmp_path)
    with patch.object(router._client, "execute", new_callable=AsyncMock) as execute:
        ack, outcomes = await _sync(router, fields={"capacity": "high"})
    assert ack is None
    execute.assert_not_awaited()
    assert (outcomes[0].kind, outcomes[0].reason) == (GraphSyncOutcomeKind.REFUSED, "row_type")


@pytest.mark.asyncio
async def test_a_typed_list_of_maps_is_sent_as_maps_never_a_json_string(tmp_path: Path) -> None:
    """P18 shape: a declared list<map> rides the row as a list of mappings."""
    router = _router(tmp_path)
    items = [{"code": "A1", "form": "flake", "undeclared": "x"}, {"code": "B2"}]
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_answer(OK)
    ) as execute:
        await _sync(router, fields={"handled_items": items})
    row = execute.await_args.kwargs["payload"]["batch"][0]
    assert row["handled_items"] == [{"code": "A1", "form": "flake"}, {"code": "B2"}]
    assert all(isinstance(item, dict) for item in row["handled_items"])


@pytest.mark.asyncio
async def test_a_batch_ceg_rejected_in_full_is_refused(tmp_path: Path) -> None:
    router = _router(tmp_path)
    rejected = {"status": "all_rejected", "entity_type": "depots", "synced_count": 0}
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_answer(rejected)
    ):
        ack, outcomes = await _sync(router)
    assert ack is None
    assert (outcomes[0].kind, outcomes[0].reason) == (GraphSyncOutcomeKind.REFUSED, "all_rejected")


@pytest.mark.parametrize(
    "answer",
    [
        {"status": "success", "entity_type": "depots", "synced_count": 2},
        {"status": "success", "entity_type": "other", "synced_count": 1},
        {"status": "success", "entity_type": "depots"},
        {"unexpected": True},
    ],
    ids=["wrong-count", "wrong-endpoint", "no-count", "malformed"],
)
@pytest.mark.asyncio
async def test_an_answer_that_does_not_confirm_this_row_is_retryable(
    tmp_path: Path, answer: dict[str, Any]
) -> None:
    """Malformed CEG response: not synced, and the pinned input may be sent again."""
    router = _router(tmp_path)
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_answer(answer)
    ):
        ack, outcomes = await _sync(router)
    assert ack is None
    assert (outcomes[0].kind, outcomes[0].reason) == (
        GraphSyncOutcomeKind.RETRYABLE,
        "unacknowledged",
    )


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (GateConnectionError("down"), GraphSyncOutcomeKind.RETRYABLE),
        (GateTimeoutError("slow"), GraphSyncOutcomeKind.RETRYABLE),
        (GateHTTPError("unavailable", status_code=503), GraphSyncOutcomeKind.RETRYABLE),
        (GateHTTPError("no route", status_code=404), GraphSyncOutcomeKind.REFUSED),
    ],
    ids=["unreachable", "timeout", "5xx", "4xx"],
)
@pytest.mark.asyncio
async def test_transport_failures_are_classified_on_transport_signals(
    tmp_path: Path, error: Exception, kind: GraphSyncOutcomeKind
) -> None:
    router = _router(tmp_path)
    with (
        patch.object(router._client, "execute", new_callable=AsyncMock, side_effect=error),
        patch("app.engines.packet_router.asyncio.sleep", new_callable=AsyncMock),
    ):
        ack, outcomes = await _sync(router)
    assert ack is None
    assert outcomes[0].kind is kind


@pytest.mark.asyncio
async def test_a_worker_failure_packet_is_retryable_not_terminal(tmp_path: Path) -> None:
    """A failure packet does not say why; the caller bounds the retries."""
    router = _router(tmp_path)
    failure = _answer({"error": "boom"}, packet_type="failure")
    with patch.object(router._client, "execute", new_callable=AsyncMock, return_value=failure):
        ack, outcomes = await _sync(router)
    assert ack is None
    assert outcomes[0].kind is GraphSyncOutcomeKind.RETRYABLE


@pytest.mark.asyncio
async def test_a_replay_of_the_pinned_input_reuses_row_and_idempotency_key(
    tmp_path: Path,
) -> None:
    """P16 transport half: a replay past Gate's cache TTL is the same MERGE, same key."""
    router = _router(tmp_path)
    fields = {"capacity": 2.5, "handled_items": [{"code": "A1"}]}
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_answer(OK)
    ) as execute:
        await _sync(router, fields=fields)
        await _sync(router, fields=fields)
    first, second = (c.kwargs for c in execute.await_args_list)
    assert first["payload"] == second["payload"]
    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["idempotency_key"].startswith(f"eie:sync:{TENANT}:d-1:")


@pytest.mark.asyncio
async def test_without_on_outcome_the_answer_is_unchanged(tmp_path: Path) -> None:
    """Compatibility: existing callers keep `dict | None`."""
    router = _router(tmp_path)
    with patch.object(router._client, "execute", new_callable=AsyncMock, return_value=_answer(OK)):
        ack = await router.notify_graph_sync(
            tenant_id=TENANT, entity_id="d-1", fields={}, domain=TENANT, node_label="Depot"
        )
    assert ack is not None and ack["entity_type"] == "depots"


@pytest.mark.parametrize(
    ("rejected", "confirmed"),
    [(0, True), (1, False), (True, False), ("0", False)],
)
@pytest.mark.asyncio
async def test_a_node_ack_with_rejected_rows_is_not_acknowledged(
    tmp_path: Path, rejected: Any, confirmed: bool
) -> None:
    """F-A1-01: a node ack confirms only with `rejected_count` absent or 0."""
    router = _router(tmp_path)
    answer = {**OK, "rejected_count": rejected}
    with patch.object(
        router._client, "execute", new_callable=AsyncMock, return_value=_answer(answer)
    ):
        ack, outcomes = await _sync(router)
    assert (ack is not None) is confirmed
    expected = GraphSyncOutcomeKind.ACKNOWLEDGED if confirmed else GraphSyncOutcomeKind.RETRYABLE
    assert outcomes[0].kind is expected
