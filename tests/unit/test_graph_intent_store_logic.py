"""A2 store logic without a database: what `pg_store` sends and how it settles.

The PostgreSQL behaviour (locks, uniqueness, ordering) is proven in
tests/integration/test_graph_intent_store_pg.py. This module pins the parts
that are decided in Python -- the statements each call issues, their order in
one transaction, the settle state machine, the duplicate and race paths -- so
they are checked on every PR, where no database runs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app.services import pg_store
from app.services.pg_models import EnrichmentResult
from app.services.pg_store import GraphIntentSpec, GraphSyncIntent

T0 = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


class _Result:
    def __init__(self, value: Any = None, rows: list[Any] | None = None) -> None:
        self._value = value
        self._rows = rows or []

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar_one(self) -> Any:
        assert self._value is not None
        return self._value

    def all(self) -> list[Any]:
        return self._rows


class _Nested:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Session:
    """Records statements; answers `execute` from a queue."""

    def __init__(self, answers: list[Any] | None = None, flush_error: Exception | None = None):
        self.answers = list(answers or [])
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.flush_error = flush_error

    async def execute(self, statement: Any, *args: Any) -> _Result:
        self.statements.append(statement)
        answer = self.answers.pop(0) if self.answers else _Result()
        return answer

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        if self.flush_error is not None:
            raise self.flush_error

    def begin_nested(self) -> _Nested:
        return _Nested(self)


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> list[_Session]:
    """Each `get_session()` hands out the next queued fake session."""
    sessions: list[_Session] = []
    issued: list[_Session] = []

    @asynccontextmanager
    async def get_session() -> AsyncIterator[_Session]:
        current = sessions.pop(0) if sessions else _Session()
        issued.append(current)
        yield current

    monkeypatch.setattr(pg_store, "get_session", get_session)
    sessions.append(_Session())
    return sessions


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _spec(**overrides: Any) -> GraphIntentSpec:
    values: dict[str, Any] = {
        "tenant_id": "synthco",
        "effect_key": "op-1",
        "entity_id": "d-1",
        "fields": {"handled_items": [{"code": "A1"}]},
        "node_label": "Depot",
        "endpoint": "depots",
        "contract_digest": "c" * 64,
    }
    values.update(overrides)
    return GraphIntentSpec(**values)


async def _save(**kwargs: Any) -> Any:
    return await pg_store.save_enrichment_result(
        tenant_id="synthco",
        entity_id="d-1",
        object_type="Depot",
        fields={"capacity": 2.5},
        confidence=0.9,
        uncertainty_score=0.1,
        tokens_used=1,
        processing_time_ms=1,
        pass_count=1,
        **kwargs,
    )


# ── Recording the intent with the result ────────────────────────────────────


async def test_the_intent_is_inserted_before_the_result_in_one_transaction(
    session: list[_Session],
) -> None:
    tx = _Session(answers=[_Result(value=uuid.uuid4())])
    session[:] = [tx]
    with pg_store.graph_intent_scope(_spec()):
        record = await _save()
    (insert,) = tx.statements
    sql = _sql(insert)
    assert sql.startswith("INSERT INTO graph_sync_intents")
    assert "ON CONFLICT ON CONSTRAINT uq_graph_sync_intents_tenant_key DO NOTHING" in sql
    assert "RETURNING graph_sync_intents.id" in sql
    params = insert.compile(dialect=postgresql.dialect()).params
    assert params["enrichment_result_id"] == record.id
    assert params["fields"] == {"handled_items": [{"code": "A1"}]}
    assert (params["state"], params["attempts"]) == ("pending", 0)
    assert tx.added == [record]


async def test_an_existing_intent_makes_the_commit_a_duplicate(session: list[_Session]) -> None:
    session[:] = [_Session(answers=[_Result(value=None)])]
    with pg_store.graph_intent_scope(_spec()), pytest.raises(pg_store.GraphIntentDuplicateError):
        await _save()


async def test_without_a_scope_no_intent_is_written(session: list[_Session]) -> None:
    tx = _Session()
    session[:] = [tx]
    await _save()
    assert tx.statements == []
    assert len(tx.added) == 1


async def test_an_intent_bound_to_another_tenant_is_refused(session: list[_Session]) -> None:
    with (
        pg_store.graph_intent_scope(_spec(tenant_id="other")),
        pytest.raises(ValueError, match="graph intent tenant"),
    ):
        await _save()


async def test_the_scope_is_reset_after_its_block() -> None:
    with pg_store.graph_intent_scope(_spec()):
        assert pg_store._graph_intent_var.get() is not None
    assert pg_store._graph_intent_var.get() is None


async def test_an_idempotent_hit_records_the_missing_intent_against_the_existing_row(
    session: list[_Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = SimpleNamespace(id=uuid.uuid4(), entity_id="d-1", fields={"capacity": 2.5})

    async def found(*_: Any, **__: Any) -> Any:
        return existing

    monkeypatch.setattr(pg_store, "get_enrichment_result_by_idempotency_key", found)
    tx = _Session(answers=[_Result(value=uuid.uuid4())])
    session[:] = [tx]
    with pg_store.graph_intent_scope(_spec()):
        assert await _save(idempotency_key="caller-1") is existing
    params = tx.statements[0].compile(dialect=postgresql.dialect()).params
    assert params["enrichment_result_id"] == existing.id


async def test_an_idempotency_race_returns_the_winner_and_rebinds_the_intent(
    session: list[_Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def none(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(pg_store, "get_enrichment_result_by_idempotency_key", none)
    winner = EnrichmentResult(
        id=uuid.uuid4(), tenant_id="synthco", entity_id="d-1", fields={"capacity": 2.5}
    )
    race = IntegrityError(
        "INSERT", {}, Exception("uq_enrichment_results_tenant_idempotency_key violated")
    )
    tx = _Session(answers=[_Result(value=uuid.uuid4()), _Result(value=winner)], flush_error=race)
    session[:] = [tx]
    with pg_store.graph_intent_scope(_spec()):
        assert await _save(idempotency_key="caller-1") is winner
    rebind = _sql(tx.statements[-1])
    assert rebind.startswith("UPDATE graph_sync_intents SET enrichment_result_id")


async def test_a_mismatched_idempotent_replay_is_a_duplicate(
    session: list[_Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = SimpleNamespace(id=uuid.uuid4(), entity_id="d-1", fields={"capacity": 1.0})

    async def found(*_: Any, **__: Any) -> Any:
        return existing

    monkeypatch.setattr(pg_store, "get_enrichment_result_by_idempotency_key", found)
    tx = _Session()
    session[:] = [tx]
    with (
        pg_store.graph_intent_scope(_spec()),
        pytest.raises(pg_store.GraphIntentDuplicateError),
    ):
        await _save(idempotency_key="caller-1")
    assert tx.statements == []


async def test_a_mismatched_race_winner_rolls_the_intent_back(
    session: list[_Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def none(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(pg_store, "get_enrichment_result_by_idempotency_key", none)
    winner = EnrichmentResult(
        id=uuid.uuid4(), tenant_id="synthco", entity_id="d-9", fields={"capacity": 2.5}
    )
    race = IntegrityError(
        "INSERT", {}, Exception("uq_enrichment_results_tenant_idempotency_key violated")
    )
    session[:] = [
        _Session(answers=[_Result(value=uuid.uuid4()), _Result(value=winner)], flush_error=race)
    ]
    with (
        pg_store.graph_intent_scope(_spec()),
        pytest.raises(pg_store.GraphIntentDuplicateError),
    ):
        await _save(idempotency_key="caller-1")


async def test_held_origins_filter_every_statement(session: list[_Session]) -> None:
    tx = _Session(answers=[_Result(), _Result(), _Result(rows=[])])
    session[:] = [tx]
    await pg_store.claim_graph_intents(
        owner="w1", lease_seconds=30, now=T0, held_origins=frozenset({"converge"})
    )
    for statement in tx.statements:
        assert "origin NOT IN" in _sql(statement)


async def test_any_other_integrity_error_is_raised(
    session: list[_Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def none(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(pg_store, "get_enrichment_result_by_idempotency_key", none)
    other = IntegrityError("INSERT", {}, Exception("some_other_constraint"))
    session[:] = [_Session(flush_error=other)]
    with pytest.raises(IntegrityError):
        await _save(idempotency_key="caller-1")


# ── Claiming ────────────────────────────────────────────────────────────────


def _row(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "seq": 1,
        "tenant_id": "synthco",
        "effect_key": "op-1",
        "entity_id": "d-1",
        "node_label": "Depot",
        "domain": "synthco",
        "endpoint": "depots",
        "contract_digest": "c" * 64,
        "cartridge_revision": None,
        "origin": "enrich",
        "fields": {"capacity": 2.5},
        "enrichment_result_id": None,
        "state": "dispatching",
        "attempts": 1,
        "max_attempts": 8,
        "next_attempt_at": T0,
        "lease_owner": "w1",
        "lease_expires_at": T0 + timedelta(seconds=30),
        "last_error": None,
        "last_ack": None,
    }
    values.update(overrides)
    return values


async def test_a_claim_supersedes_exhausts_then_leases_in_seq_order(
    session: list[_Session],
) -> None:
    later, earlier = _row(seq=7, effect_key="b"), _row(seq=3, effect_key="a")
    tx = _Session(answers=[_Result(), _Result(), _Result(rows=[later, earlier])])
    session[:] = [tx]
    claimed = await pg_store.claim_graph_intents(owner="w1", lease_seconds=30, now=T0)
    assert [r.effect_key for r in claimed] == ["a", "b"]
    supersede, exhaust, claim = (_sql(s) for s in tx.statements)
    assert supersede.startswith("UPDATE graph_sync_intents SET state=")
    assert "EXISTS (SELECT" in supersede and "graph_sync_intents_1.seq >" in supersede
    assert "graph_sync_intents_1.attempts >" in supersede  # "sent" = claimed at least once
    assert "graph_sync_intents_1.endpoint IS NOT DISTINCT FROM" in supersede
    assert "graph_sync_intents.attempts >= graph_sync_intents.max_attempts" in exhaust
    assert "FOR UPDATE SKIP LOCKED" in claim
    assert claim.count("NOT (EXISTS (SELECT") == 2  # no older outstanding, no newer sent
    assert "attempts=(graph_sync_intents.attempts +" in claim
    assert "RETURNING" in claim
    params = tx.statements[2].compile(dialect=postgresql.dialect()).params
    assert params["lease_owner"].startswith("w1#")
    again = _Session(answers=[_Result(), _Result(), _Result(rows=[])])
    session[:] = [again]
    await pg_store.claim_graph_intents(owner="w1", lease_seconds=30, now=T0)
    token = again.statements[2].compile(dialect=postgresql.dialect()).params["lease_owner"]
    assert token != params["lease_owner"], "every claim gets its own fencing token"
    assert params["lease_expires_at"] == T0 + timedelta(seconds=30)


async def test_a_single_operation_claim_is_scoped_to_its_key(session: list[_Session]) -> None:
    tx = _Session(answers=[_Result(), _Result(), _Result(rows=[])])
    session[:] = [tx]
    assert (
        await pg_store.claim_graph_intents(
            owner="w1", lease_seconds=30, tenant_id="synthco", effect_key="op-1", now=T0
        )
        == []
    )
    for statement in tx.statements:
        params = statement.compile(dialect=postgresql.dialect()).params
        assert "synthco" in params.values() and "op-1" in params.values()


async def test_a_half_scoped_claim_is_refused() -> None:
    with pytest.raises(ValueError, match="both tenant_id and effect_key"):
        await pg_store.claim_graph_intents(owner="w1", lease_seconds=30, tenant_id="synthco")


async def test_get_graph_intent_reads_one_operation(session: list[_Session]) -> None:
    row = GraphSyncIntent(**_row(state="pending"))
    session[:] = [_Session(answers=[_Result(value=row)])]
    record = await pg_store.get_graph_intent("synthco", "op-1")
    assert record is not None and record.state == "pending" and not record.terminal
    session[:] = [_Session(answers=[_Result(value=None)])]
    assert await pg_store.get_graph_intent("synthco", "missing") is None


# ── Settling ────────────────────────────────────────────────────────────────


def _leased(**overrides: Any) -> GraphSyncIntent:
    return GraphSyncIntent(**_row(**overrides))


async def _settle(session: list[_Session], row: Any, **kwargs: Any) -> str | None:
    session[:] = [_Session(answers=[_Result(value=row)])]
    return await pg_store.settle_graph_intent(uuid.uuid4(), owner="w1", now=T0, **kwargs)


async def test_acknowledged_records_the_ack_and_resolves(session: list[_Session]) -> None:
    row = _leased()
    state = await _settle(session, row, outcome="acknowledged", ack={"synced_count": 1})
    assert state == "acknowledged"
    assert row.last_ack == {"synced_count": 1} and row.resolved_at == T0
    assert row.lease_owner is None and row.lease_expires_at is None


async def test_terminal_records_the_reason(session: list[_Session]) -> None:
    row = _leased()
    assert await _settle(session, row, outcome="terminal", error="refused:row_type") == (
        "failed_terminal"
    )
    assert row.last_error == "refused:row_type" and row.resolved_at == T0


async def test_retry_backs_off_from_the_attempt_count(session: list[_Session]) -> None:
    row = _leased(attempts=3)
    state = await _settle(
        session, row, outcome="retry", error="unacknowledged", retry_base_seconds=2
    )
    assert state == "pending"
    assert row.next_attempt_at == T0 + timedelta(seconds=8)
    assert row.resolved_at is None


async def test_retry_past_the_ceiling_is_terminal(session: list[_Session]) -> None:
    row = _leased(attempts=8, max_attempts=8)
    assert await _settle(session, row, outcome="retry", error="unacknowledged") == (
        "failed_terminal"
    )
    assert row.last_error == "retry_exhausted:unacknowledged"


async def test_a_lost_lease_settles_nothing(session: list[_Session]) -> None:
    assert await _settle(session, None, outcome="acknowledged") is None


async def test_an_unknown_outcome_is_refused(session: list[_Session]) -> None:
    with pytest.raises(ValueError, match="unknown graph intent outcome"):
        await _settle(session, _leased(), outcome="maybe")


def test_backoff_doubles_and_caps() -> None:
    delays = [pg_store.graph_intent_backoff(n, base_seconds=2, cap_seconds=20) for n in range(6)]
    assert delays == [2, 2, 4, 8, 16, 20]


async def test_status_counts_per_state(session: list[_Session]) -> None:
    tx = _Session(answers=[_Result(rows=[("pending", 2), ("acknowledged", 5)])])
    session[:] = [tx]
    assert await pg_store.graph_intent_status_counts("synthco") == {
        "pending": 2,
        "acknowledged": 5,
    }
    assert "WHERE graph_sync_intents.tenant_id" in _sql(tx.statements[0])
