"""A4 on PostgreSQL: graph-return targets are retained, deduplicated, leased, settled.

Needs a PostgreSQL the test may own: `EIE_TEST_DATABASE_URL`
(`postgresql+asyncpg://...`). Every test drops and recreates the schema.
Without it the module skips -- or fails under `EIE_REQUIRE_INTEGRATION=1`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from constellation_node_sdk.transport import create_transport_packet
from sqlalchemy import select

from app.services import graph_return_channel as grc
from app.services import pg_store
from app.services.graph_return_channel import (
    GraphReturnCapacityError,
    GraphReturnChannel,
    handle_graph_inference_result,
)
from tests.conftest import skip_or_fail_unavailable

pytestmark = pytest.mark.integration

TENANT = "synthco"
OTHER = "othertenant"
DB_URL = os.environ.get("EIE_TEST_DATABASE_URL", "")


@pytest_asyncio.fixture
async def db() -> AsyncIterator[None]:
    if not DB_URL:
        skip_or_fail_unavailable("EIE_TEST_DATABASE_URL not set")
    pg_store.init_engine(DB_URL, pool_size=5, max_overflow=5)
    engine = pg_store._engine
    assert engine is not None
    try:
        async with engine.begin() as conn:
            await conn.run_sync(pg_store.Base.metadata.drop_all)
            await conn.run_sync(pg_store.Base.metadata.create_all)
    except OSError as exc:
        await pg_store.close_engine()
        skip_or_fail_unavailable(f"PostgreSQL unreachable: {exc}")
    GraphReturnChannel.reset_instance()
    yield
    GraphReturnChannel.reset_instance()
    await pg_store.close_engine()


def _output(field: str, value: Any, confidence: float = 0.9, **extra: Any) -> dict[str, Any]:
    return {
        "entity_id": extra.pop("entity_id", "d-1"),
        "field": field,
        "value": value,
        "confidence": confidence,
        "rule": extra.pop("rule", "r1"),
        **extra,
    }


def _packet(outputs: list[dict[str, Any]], *, tenant: str = TENANT, key: str | None = "p-1"):
    return create_transport_packet(
        action="graph-inference-result",
        payload={"inference_outputs": outputs},
        tenant=tenant,
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
        idempotency_key=key,
    )


async def _rows(**where: Any) -> list[pg_store.GraphReturnTarget]:
    stmt = select(pg_store.GraphReturnTarget).order_by(pg_store.GraphReturnTarget.seq)
    for name, value in where.items():
        stmt = stmt.where(getattr(pg_store.GraphReturnTarget, name) == value)
    async with pg_store.get_session() as session:
        return list((await session.execute(stmt)).scalars())


@pytest.mark.asyncio
async def test_a_retransmitted_packet_is_retained_once_and_answered_the_same(db: None) -> None:
    packet = _packet([_output("capacity", 2.5), _output("region", "north")])
    first = await handle_graph_inference_result(TENANT, packet.payload, packet)
    second = await handle_graph_inference_result(TENANT, packet.payload, packet)
    assert first["targets_queued"] == second["targets_queued"] == 2
    rows = await _rows()
    assert [(r.field_name, r.state) for r in rows] == [
        ("capacity", "retained"),
        ("region", "retained"),
    ]


@pytest.mark.asyncio
async def test_concurrent_deliveries_of_one_packet_record_one_set_of_rows(db: None) -> None:
    packet = _packet([_output("capacity", 2.5)])
    channel = GraphReturnChannel.get_instance()
    counts = await asyncio.gather(*(channel.submit(packet) for _ in range(5)))
    assert counts == [1, 1, 1, 1, 1]
    assert len(await _rows()) == 1


@pytest.mark.asyncio
async def test_a_target_accepted_before_a_restart_is_consumed_after_it(db: None) -> None:
    await GraphReturnChannel.get_instance().submit(_packet([_output("capacity", 2.5)]))
    GraphReturnChannel.reset_instance()  # the process restarts; memory is gone
    claim = await GraphReturnChannel.get_instance().claim_for_entity(TENANT, "d-1")
    assert claim.durable
    assert [(t.field_name, t.seed_value) for t in claim.targets] == [("capacity", 2.5)]
    target = claim.targets[0]
    settled = await GraphReturnChannel.get_instance().settle(
        claim,
        outcomes={str(target.intake_id): "seeded"},
        rejections={},
        consumed_by="converge:op-9",
    )
    assert settled == 1
    (row,) = await _rows()
    assert (row.state, row.outcome, row.consumed_by) == ("consumed", "seeded", "converge:op-9")
    # A consumed target is not offered again.
    assert (await GraphReturnChannel.get_instance().claim_for_entity(TENANT, "d-1")).targets == []


@pytest.mark.asyncio
async def test_two_workers_never_lease_the_same_target(db: None) -> None:
    await GraphReturnChannel.get_instance().submit(
        _packet([_output(f"f{i}", i) for i in range(20)])
    )
    worker_a, worker_b = GraphReturnChannel(), GraphReturnChannel()
    claims = await asyncio.gather(
        worker_a.claim_for_entity(TENANT, "d-1", max_targets=12),
        worker_b.claim_for_entity(TENANT, "d-1", max_targets=12),
    )
    ids = [t.intake_id for c in claims for t in c.targets]
    assert len(ids) == len(set(ids)) == 20
    assert claims[0].owner != claims[1].owner


@pytest.mark.asyncio
async def test_a_dead_converge_leaves_its_targets_to_lease_expiry_then_loses_them(
    db: None,
) -> None:
    await GraphReturnChannel.get_instance().submit(_packet([_output("capacity", 2.5)]))
    (first,) = await pg_store.claim_graph_return_targets(
        tenant_id=TENANT, entity_id="d-1", owner="w1", lease_seconds=60
    )
    later = datetime.now(UTC) + timedelta(seconds=120)
    (second,) = await pg_store.claim_graph_return_targets(
        tenant_id=TENANT, entity_id="d-1", owner="w2", lease_seconds=60, now=later
    )
    assert second.id == first.id and second.attempts == 2
    assert first.lease_owner is not None
    # The first claim's settle is fenced out.
    assert (
        await pg_store.settle_graph_return_targets(
            owner=first.lease_owner, consumed={first.id: "seeded"}, rejected={}, consumed_by=None
        )
        == 0
    )
    assert second.lease_owner is not None
    assert (
        await pg_store.settle_graph_return_targets(
            owner=second.lease_owner, consumed={second.id: "seeded"}, rejected={}, consumed_by=None
        )
        == 1
    )


@pytest.mark.asyncio
async def test_targets_never_settled_expire_after_their_attempt_ceiling(db: None) -> None:
    await GraphReturnChannel.get_instance().submit(_packet([_output("capacity", 2.5)]))
    at = datetime.now(UTC)
    for attempt in range(pg_store.GRAPH_RETURN_DEFAULT_MAX_ATTEMPTS):
        claimed = await pg_store.claim_graph_return_targets(
            tenant_id=TENANT,
            entity_id="d-1",
            owner="w",
            lease_seconds=1,
            now=at + timedelta(seconds=10 * attempt),
        )
        assert len(claimed) == 1
    assert (
        await pg_store.claim_graph_return_targets(
            tenant_id=TENANT,
            entity_id="d-1",
            owner="w",
            lease_seconds=1,
            now=at + timedelta(hours=1),
        )
        == []
    )
    (row,) = await _rows()
    assert (row.state, row.reason) == ("expired", "attempts_exhausted")


@pytest.mark.asyncio
async def test_a_late_target_no_converge_reached_expires_with_its_reason(db: None) -> None:
    await GraphReturnChannel.get_instance().submit(_packet([_output("capacity", 2.5)]))
    after_ttl = datetime.now(UTC) + timedelta(seconds=grc.GRAPH_RETURN_TTL_SECONDS + 1)
    assert (
        await pg_store.claim_graph_return_targets(
            tenant_id=TENANT, entity_id="d-1", owner="w", lease_seconds=60, now=after_ttl
        )
        == []
    )
    (row,) = await _rows()
    assert (row.state, row.reason) == ("expired", "ttl_expired")


@pytest.mark.asyncio
async def test_an_echo_of_an_accepted_fact_is_recorded_unchanged_not_retained(db: None) -> None:
    channel = GraphReturnChannel.get_instance()
    assert await channel.submit(_packet([_output("capacity", 2.5, 0.8)], key="p-1")) == 1
    # Same fact, new producer key: an echo, not a new observation.
    assert await channel.submit(_packet([_output("capacity", 2.5, 0.8)], key="p-2")) == 0
    # The same value at higher confidence, and a changed value, are new.
    assert await channel.submit(_packet([_output("capacity", 2.5, 0.95)], key="p-3")) == 1
    assert await channel.submit(_packet([_output("capacity", 3.0, 0.7)], key="p-4")) == 1
    states = [(r.producer_key, r.state, r.reason) for r in await _rows()]
    assert states == [
        ("p-1", "retained", None),
        ("p-2", "rejected", "unchanged_fact"),
        ("p-3", "retained", None),
        ("p-4", "retained", None),
    ]


@pytest.mark.asyncio
async def test_ineligible_outputs_are_recorded_rejected_with_their_reason(db: None) -> None:
    packet = _packet(
        [
            _output("low", 1, 0.3),
            _output("foreign", 1, tenant_id=OTHER),
            _output("kept", 1),
            {"field": "no_entity", "value": 1, "confidence": 0.9},
        ]
    )
    assert await GraphReturnChannel.get_instance().submit(packet) == 1
    rows = {r.field_name: (r.state, r.reason) for r in await _rows()}
    assert rows == {
        "low": ("rejected", "below_threshold"),
        "foreign": ("rejected", "cross_tenant"),
        "kept": ("retained", None),
    }


@pytest.mark.asyncio
async def test_a_tenant_at_capacity_refuses_the_whole_packet(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(grc, "GRAPH_RETURN_TENANT_CAPACITY", 2)
    channel = GraphReturnChannel.get_instance()
    assert await channel.submit(_packet([_output("a", 1)], key="p-1")) == 1
    with pytest.raises(GraphReturnCapacityError):
        await channel.submit(_packet([_output("b", 1), _output("c", 1)], key="p-2"))
    assert [r.field_name for r in await _rows()] == ["a"]
    # The other tenant's capacity is its own.
    assert await channel.submit(_packet([_output("b", 1)], tenant=OTHER, key="p-2")) == 1
    # Once consumed, the tenant has room again; the refused packet is retried whole.
    claim = await channel.claim_for_entity(TENANT, "d-1")
    await channel.settle(
        claim,
        outcomes={str(t.intake_id): "seeded" for t in claim.targets},
        rejections={},
        consumed_by=None,
    )
    assert await channel.submit(_packet([_output("b", 1), _output("c", 1)], key="p-2")) == 2


@pytest.mark.asyncio
async def test_targets_never_cross_a_tenant_or_entity(db: None) -> None:
    channel = GraphReturnChannel.get_instance()
    await channel.submit(_packet([_output("capacity", 1, entity_id="d-1")], key="a"))
    await channel.submit(_packet([_output("capacity", 2, entity_id="d-2")], key="b"))
    await channel.submit(_packet([_output("capacity", 3, entity_id="d-1")], tenant=OTHER, key="c"))
    claim = await channel.claim_for_entity(TENANT, "d-1")
    assert [(t.tenant_id, t.entity_id, t.seed_value) for t in claim.targets] == [(TENANT, "d-1", 1)]


@pytest.mark.asyncio
async def test_a_converge_refusal_is_recorded_and_unsettled_targets_return(db: None) -> None:
    channel = GraphReturnChannel.get_instance()
    await channel.submit(_packet([_output("undeclared", 1), _output("kept", 2), _output("x", 3)]))
    claim = await channel.claim_for_entity(TENANT, "d-1")
    by_field = {t.field_name: str(t.intake_id) for t in claim.targets}
    settled = await channel.settle(
        claim,
        outcomes={by_field["kept"]: "kept_entity_value"},
        rejections={by_field["undeclared"]: "field_not_in_schema"},
        consumed_by="converge:op-1",
    )
    assert settled == 2
    rows = {r.field_name: (r.state, r.outcome, r.reason) for r in await _rows()}
    assert rows["undeclared"] == ("rejected", None, "field_not_in_schema")
    assert rows["kept"] == ("consumed", "kept_entity_value", None)
    assert rows["x"][0] == "consuming"  # unsettled: left to lease expiry
    counts = await pg_store.graph_return_status_counts(TENANT)
    assert counts == {"rejected": 1, "consumed": 1, "consuming": 1}
