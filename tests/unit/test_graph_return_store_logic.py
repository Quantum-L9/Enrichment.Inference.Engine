"""A4 store logic without a database: what `pg_store` sends for graph-return targets.

The PostgreSQL behaviour (locks, uniqueness, leases) is proven in
tests/integration/test_graph_return_store_pg.py. This module pins the parts
decided in Python -- the intake classification (duplicate, unchanged,
rejected, retained), the capacity refusal, the claim and settle statements --
so they are checked on every PR, where no database runs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.services import pg_store
from app.services.graph_return_channel import GraphReturnCapacityError
from app.services.pg_store import GraphReturnIntake

T0 = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
TENANT = "synthco"


class _Result:
    def __init__(self, value: Any = None, rows: list[Any] | None = None) -> None:
        self._value = value
        self._rows = rows or []

    def scalar_one(self) -> Any:
        return self._value

    def all(self) -> list[Any]:
        return self._rows


class _Session:
    def __init__(self, answers: list[Any] | None = None) -> None:
        self.answers = list(answers or [])
        self.statements: list[Any] = []

    async def execute(self, statement: Any, *args: Any) -> _Result:
        self.statements.append(statement)
        return self.answers.pop(0) if self.answers else _Result()


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> list[_Session]:
    queue: list[_Session] = []

    @asynccontextmanager
    async def get_session() -> AsyncIterator[_Session]:
        yield queue.pop(0) if queue else _Session()

    monkeypatch.setattr(pg_store, "get_session", get_session)
    return queue


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _params(statement: Any) -> dict[str, Any]:
    return dict(statement.compile(dialect=postgresql.dialect()).params)


def _intake(field: str = "capacity", value: Any = 2.5, **overrides: Any) -> GraphReturnIntake:
    values: dict[str, Any] = {
        "tenant_id": TENANT,
        "entity_id": "d-1",
        "field_name": field,
        "rule": "r1",
        "value": value,
        "confidence": 0.9,
        "producer_key": "p-1",
        "origin_packet_id": "pkt-1",
    }
    values.update(overrides)
    return GraphReturnIntake(**values)


def test_the_intake_key_is_the_producer_key_plus_the_fact() -> None:
    base = _intake()
    assert base.intake_key == _intake().intake_key
    assert base.fact_key == _intake(producer_key="p-2").fact_key
    assert base.intake_key != _intake(producer_key="p-2").intake_key
    for changed in (
        _intake(value=3.0),
        _intake(field="region"),
        _intake(rule="r2"),
        _intake(entity_id="d-2"),
        _intake(tenant_id="other"),
    ):
        assert changed.fact_key != base.fact_key
    # Confidence is not part of the fact: it decides "unchanged", not identity.
    assert _intake(confidence=0.6).fact_key == base.fact_key
    # A structured value is keyed by its canonical JSON, not its key order.
    assert _intake(value={"a": 1, "b": 2}).fact_key == _intake(value={"b": 2, "a": 1}).fact_key


@pytest.mark.asyncio
async def test_an_empty_packet_records_nothing(sessions: list[_Session]) -> None:
    result = await pg_store.retain_graph_return_targets([], capacity=10, ttl_seconds=60)
    assert result == pg_store.GraphReturnIntakeResult(0, 0, 0, 0)


@pytest.mark.asyncio
async def test_outputs_of_two_tenants_are_not_one_packet() -> None:
    with pytest.raises(ValueError, match="one tenant"):
        await pg_store.retain_graph_return_targets(
            [_intake(), _intake(tenant_id="other")], capacity=10, ttl_seconds=60
        )


@pytest.mark.asyncio
async def test_intake_locks_the_tenant_and_classifies_each_output(
    sessions: list[_Session],
) -> None:
    dup, echo, fresh, refused = (
        _intake("dup"),
        _intake("echo"),
        _intake("fresh"),
        _intake("low", rejection="below_threshold"),
    )
    tx = _Session(
        [
            _Result(),  # advisory lock
            _Result(rows=[(dup.intake_key, "consumed")]),  # recorded intake keys
            _Result(rows=[(echo.fact_key, 0.9)]),  # facts already accepted
            _Result(value=3),  # open rows of the tenant
            _Result(),  # insert
        ]
    )
    sessions.append(tx)
    result = await pg_store.retain_graph_return_targets(
        [dup, echo, fresh, refused, fresh], capacity=10, ttl_seconds=60, now=T0
    )
    assert result == pg_store.GraphReturnIntakeResult(
        retained=2, duplicates=2, unchanged=1, rejected=1
    )
    assert "pg_advisory_xact_lock" in _sql(tx.statements[0])
    assert _params(tx.statements[0])["hashtextextended_1"] == f"graph_return|{TENANT}"
    insert = tx.statements[4]
    assert "ON CONFLICT ON CONSTRAINT uq_graph_return_targets_tenant_intake DO NOTHING" in _sql(
        insert
    )
    rows = {
        params["field_name"]: params
        for params in (
            {k.rsplit("_m", 1)[0]: v for k, v in _params(insert).items() if k.endswith(f"_m{i}")}
            for i in range(3)
        )
    }
    assert (rows["echo"]["state"], rows["echo"]["reason"]) == ("rejected", "unchanged_fact")
    assert (rows["low"]["state"], rows["low"]["reason"]) == ("rejected", "below_threshold")
    assert (rows["fresh"]["state"], rows["fresh"]["reason"]) == ("retained", None)
    assert rows["fresh"]["value"] == {"value": 2.5}
    assert rows["fresh"]["expires_at"] == T0.replace(minute=1)


@pytest.mark.asyncio
async def test_a_weaker_copy_of_an_accepted_fact_is_unchanged_a_stronger_one_is_new(
    sessions: list[_Session],
) -> None:
    weaker, stronger = _intake("a", confidence=0.7), _intake("b", confidence=0.95)
    sessions.append(
        _Session(
            [
                _Result(),
                _Result(rows=[]),
                _Result(rows=[(weaker.fact_key, 0.8), (stronger.fact_key, 0.8)]),
                _Result(value=0),
                _Result(),
            ]
        )
    )
    result = await pg_store.retain_graph_return_targets(
        [weaker, stronger], capacity=10, ttl_seconds=60
    )
    assert (result.retained, result.unchanged) == (1, 1)


@pytest.mark.asyncio
async def test_a_packet_past_capacity_raises_before_inserting(sessions: list[_Session]) -> None:
    tx = _Session([_Result(), _Result(rows=[]), _Result(rows=[]), _Result(value=9)])
    sessions.append(tx)
    with pytest.raises(GraphReturnCapacityError) as refused:
        await pg_store.retain_graph_return_targets(
            [_intake("a"), _intake("b")], capacity=10, ttl_seconds=60
        )
    assert (refused.value.open_count, refused.value.incoming, refused.value.capacity) == (9, 2, 10)
    assert len(tx.statements) == 4  # no insert was issued


@pytest.mark.asyncio
async def test_a_packet_of_only_rejections_never_counts_capacity(
    sessions: list[_Session],
) -> None:
    tx = _Session([_Result(), _Result(rows=[]), _Result()])
    sessions.append(tx)
    result = await pg_store.retain_graph_return_targets(
        [_intake(rejection="cross_tenant")], capacity=0, ttl_seconds=60
    )
    assert (result.retained, result.rejected) == (0, 1)
    assert "count" not in _sql(tx.statements[-1]).lower()


def _row(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "seq": 1,
        "tenant_id": TENANT,
        "entity_id": "d-1",
        "field_name": "capacity",
        "rule": "r1",
        "value": {"value": 2.5},
        "confidence": 0.9,
        "producer_key": "p-1",
        "origin_packet_id": "pkt-1",
        "state": "consuming",
        "attempts": 1,
        "max_attempts": 5,
        "lease_owner": "converge#abc",
        "lease_expires_at": T0,
        "expires_at": T0,
        "created_at": T0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_a_claim_expires_dead_rows_then_leases_the_rest_under_a_fresh_token(
    sessions: list[_Session],
) -> None:
    tx = _Session([_Result(), _Result(), _Result(rows=[_row(seq=2), _row(seq=1)])])
    sessions.append(tx)
    claimed = await pg_store.claim_graph_return_targets(
        tenant_id=TENANT, entity_id="d-1", owner="converge", lease_seconds=300, now=T0
    )
    assert [r.seq for r in claimed] == [1, 2]
    assert claimed[0].value == 2.5
    ttl, attempts, claim = (_sql(s) for s in tx.statements)
    assert "expires_at <=" in ttl and _params(tx.statements[0])["reason"] == "ttl_expired"
    assert "attempts >= graph_return_targets.max_attempts" in attempts
    assert _params(tx.statements[1])["reason"] == "attempts_exhausted"
    assert "FOR UPDATE SKIP LOCKED" in claim
    assert "attempts + " in claim
    params = _params(tx.statements[2])
    assert params["state"] == "consuming"
    owner = params["lease_owner"]
    assert owner.startswith("converge#") and len(owner) == len("converge#") + 16


@pytest.mark.asyncio
async def test_settle_is_fenced_by_the_claim_token(sessions: list[_Session]) -> None:
    seeded, refused = uuid.uuid4(), uuid.uuid4()
    tx = _Session([_Result(rows=[(seeded,)]), _Result(rows=[])])
    sessions.append(tx)
    settled = await pg_store.settle_graph_return_targets(
        owner="converge#abc",
        consumed={seeded: "seeded"},
        rejected={refused: "field_not_in_schema"},
        consumed_by="converge:op-1",
        now=T0,
    )
    assert settled == 1
    consumed_sql, rejected_sql = (_sql(s) for s in tx.statements)
    for sql in (consumed_sql, rejected_sql):
        assert "graph_return_targets.lease_owner = " in sql
        assert "graph_return_targets.state = " in sql
    consumed_params, rejected_params = (_params(s) for s in tx.statements)
    assert (consumed_params["state"], consumed_params["outcome"]) == ("consumed", "seeded")
    assert consumed_params["consumed_by"] == "converge:op-1"
    assert (rejected_params["state"], rejected_params["reason"]) == (
        "rejected",
        "field_not_in_schema",
    )


@pytest.mark.asyncio
async def test_status_counts_group_by_state(sessions: list[_Session]) -> None:
    tx = _Session([_Result(rows=[("retained", 3), ("consumed", 1)])])
    sessions.append(tx)
    assert await pg_store.graph_return_status_counts(TENANT) == {"retained": 3, "consumed": 1}
    assert "GROUP BY graph_return_targets.state" in _sql(tx.statements[0])
