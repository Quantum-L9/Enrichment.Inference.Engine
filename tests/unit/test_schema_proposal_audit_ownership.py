"""Owner-qualified ledger contracts; fake sessions are NOT PostgreSQL race proof."""

from __future__ import annotations

import copy
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Insert

from app.services import pg_store
from app.services.pg_models import SchemaProposalRecord

pytestmark = pytest.mark.unit
_DIGEST = "a" * 64


def _decision(node: str = "Facility", approved: bool = True, **changes: Any) -> dict[str, Any]:
    return {
        "node_label": node,
        "discovery_spec_sha256": _DIGEST,
        "ownership_contract_version": 1,
        "field_name": "shared_metric",
        "field_type": "float",
        "source": "enrichment",
        "fill_rate": 1.0,
        "avg_confidence": 0.9,
        "sample_values": [42],
        "value_distribution": {"42": 1},
        "approved": approved,
        **changes,
    }


class _Session:
    """Model insert-conflict + reload contracts, without connecting to a database."""

    def __init__(self) -> None:
        self.rows: dict[tuple[Any, ...], SchemaProposalRecord] = {}
        self.statements: list[Any] = []
        self.opens = 0
        self.commits = 0
        self.rollbacks = 0
        self.fail_insert = 0
        self.inserts = 0
        self.concurrent_row: SchemaProposalRecord | None = None

    @asynccontextmanager
    async def context(self) -> AsyncIterator[_Session]:
        self.opens += 1
        before = copy.deepcopy(self.rows)
        try:
            yield self
            self.commits += 1
        except BaseException:
            self.rows = before
            self.rollbacks += 1
            raise

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        compiled = statement.compile(dialect=postgresql.dialect())
        if isinstance(statement, Insert):
            self.inserts += 1
            if self.inserts == self.fail_insert:
                raise RuntimeError("simulated insert failure")
            params = dict(compiled.params)
            key = tuple(
                params[k]
                for k in ("tenant_id", "domain", "batch_run_id", "node_label", "field_name")
            )
            if key not in self.rows:
                row = self.concurrent_row or SchemaProposalRecord(**params)
                self.concurrent_row = None
                if row.id is None:
                    row.id = uuid.uuid4()
                if row.created_at is None:
                    row.created_at = datetime(2026, 1, 1, tzinfo=UTC)
                self.rows[key] = row
            return SimpleNamespace(scalar_one_or_none=lambda: None)
        params = compiled.params
        candidates = list(self.rows.values())
        for name in (
            "id",
            "tenant_id",
            "domain",
            "batch_run_id",
            "node_label",
            "field_name",
            "approval_status",
        ):
            if f"{name}_1" in params:
                candidates = [r for r in candidates if getattr(r, name) == params[f"{name}_1"]]
        row = candidates[0] if candidates else None
        return SimpleNamespace(
            scalar_one_or_none=lambda: row,
            scalars=lambda: SimpleNamespace(all=lambda: candidates),
        )

    async def flush(self) -> None:
        return None


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> _Session:
    session = _Session()
    monkeypatch.setattr(pg_store, "get_session", session.context)
    return session


async def _persist(decisions: list[dict[str, Any]], **changes: Any) -> list[SchemaProposalRecord]:
    return await pg_store.persist_schema_proposal_batch(
        **{
            "tenant_id": "plasticos",
            "domain": "plasticos",
            "batch_run_id": "batch-1",
            "reviewed_by": "reviewer",
            "decisions": decisions,
            **changes,
        }
    )


async def _save(**changes: Any) -> SchemaProposalRecord:
    values = _decision(**changes)
    values.pop("approved")
    return await pg_store.save_schema_proposal(
        tenant_id="plasticos", domain="plasticos", batch_run_id="batch-1", **values
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"node_label": None},
        {"node_label": ""},
        {"node_label": "has space"},
        {"node_label": "N" * 129},
        {"discovery_spec_sha256": None},
        {"discovery_spec_sha256": "A" * 64},
        {"discovery_spec_sha256": "a" * 63},
        {"ownership_contract_version": None},
        {"ownership_contract_version": 2},
        {"ownership_contract_version": True},
        {"field_name": "badName"},
        {"field_name": "null_semantics"},
        {"field_name": "f" * 257},
        {"approved": 1},
        {"approved": "false"},
        {"field_type": ""},
        {"source": ""},
        {"fill_rate": float("nan")},
        {"avg_confidence": 2},
        {"sample_values": {"not": "a list"}},
        {"value_distribution": []},
        {"tenant_id": "other"},
        {"domain": "other"},
        {"batch_run_id": "other"},
    ],
)
async def test_entire_batch_refused_before_session(
    ledger: _Session, changes: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        await _persist([_decision("Alpha"), _decision("Beta", **changes)])
    assert ledger.opens == 0
    assert not ledger.rows


@pytest.mark.parametrize(
    "changes",
    [
        {"domain": "other"},
        {"tenant_id": " bad"},
        {"batch_run_id": ""},
        {"batch_run_id": "b" * 257},
        {"reviewed_by": ""},
        {"reviewed_by": " r "},
        {"reviewed_by": "r" * 257},
    ],
)
async def test_scope_and_review_validation_precedes_session(
    ledger: _Session, changes: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        await _persist([_decision()], **changes)
    assert ledger.opens == 0


async def test_duplicate_pair_is_refused_without_writes(ledger: _Session) -> None:
    with pytest.raises(ValueError, match="Duplicate schema proposal identity"):
        await _persist([_decision(), _decision(approved=False)])
    assert ledger.opens == 0


async def test_two_owners_same_field_are_distinct_and_retries_preserve_review(
    ledger: _Session,
) -> None:
    rows = await _persist([_decision("Alpha"), _decision("Beta", False)])
    original = [(r.id, r.created_at, r.reviewed_at, r.reviewed_by) for r in rows]
    assert [(r.node_label, r.approval_status) for r in rows] == [
        ("Alpha", "approved"),
        ("Beta", "rejected"),
    ]
    retried = await _persist([_decision("Alpha"), _decision("Beta", False)])
    assert [(r.id, r.created_at, r.reviewed_at, r.reviewed_by) for r in retried] == original
    assert len(ledger.rows) == 2
    assert ledger.opens == ledger.commits == 2
    sql = str(ledger.statements[0].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (tenant_id, domain, batch_run_id, node_label, field_name)" in sql
    assert "WHERE ownership_contract_version" in sql and "DO NOTHING" in sql
    assert "DO UPDATE" not in sql
    reload_sql = str(ledger.statements[1].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in reload_sql
    for name in ("tenant_id", "domain", "batch_run_id", "node_label", "field_name"):
        assert f"schema_proposals.{name} =" in reload_sql


@pytest.mark.parametrize(
    "changes",
    [
        {"approved": False},
        {"discovery_spec_sha256": "b" * 64},
        {"sample_values": [43]},
        {"field_type": "string"},
        {"yaml_diff": "different"},
        {"avg_confidence": 0.8},
    ],
)
async def test_conflicting_retry_does_not_restamp_or_override(
    ledger: _Session, changes: dict[str, Any]
) -> None:
    row = (await _persist([_decision()]))[0]
    before = (
        row.id,
        row.created_at,
        row.reviewed_at,
        row.reviewed_by,
        row.approval_status,
        row.discovery_spec_sha256,
    )
    with pytest.raises(ValueError, match="Conflicting schema proposal retry"):
        await _persist([_decision(**changes)])
    current = next(iter(ledger.rows.values()))
    assert (
        current.id,
        current.created_at,
        current.reviewed_at,
        current.reviewed_by,
        current.approval_status,
        current.discovery_spec_sha256,
    ) == before
    assert ledger.rollbacks == 1


async def test_different_retry_reviewer_is_refused(ledger: _Session) -> None:
    await _persist([_decision()])
    with pytest.raises(ValueError, match="Conflicting schema proposal retry"):
        await _persist([_decision()], reviewed_by="another-reviewer")


async def test_whole_batch_rolls_back_on_late_insert_failure(ledger: _Session) -> None:
    ledger.fail_insert = 2
    with pytest.raises(RuntimeError, match="simulated insert failure"):
        await _persist([_decision("Alpha"), _decision("Beta")])
    assert ledger.rows == {}
    assert ledger.opens == ledger.rollbacks == 1
    assert ledger.commits == 0


async def test_conflict_reload_compares_winning_row_not_incoming_payload(ledger: _Session) -> None:
    ledger.concurrent_row = SchemaProposalRecord(
        tenant_id="plasticos",
        domain="plasticos",
        batch_run_id="batch-1",
        **{k: v for k, v in _decision(sample_values=[99]).items() if k != "approved"},
        proposed_gate=None,
        proposed_scoring_dimension=None,
        yaml_diff=None,
        approval_status="approved",
        reviewed_by="reviewer",
        reviewed_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="Conflicting schema proposal retry"):
        await _persist([_decision()])
    assert ledger.rollbacks == 1


async def test_pending_save_requires_binding_and_is_immutable(ledger: _Session) -> None:
    row = await _save()
    assert row.approval_status == "pending" and row.reviewed_at is None
    assert (await _save()).id == row.id
    with pytest.raises(ValueError, match="Conflicting schema proposal retry"):
        await _save(sample_values=[43])
    assert next(iter(ledger.rows.values())).approval_status == "pending"


async def test_unbound_save_refuses_before_session(ledger: _Session) -> None:
    with pytest.raises(ValueError, match="legacy-unbound"):
        await pg_store.save_schema_proposal(
            "plasticos", "plasticos", "batch", "metric", "float", "enrichment", 1.0, 0.9
        )
    assert ledger.opens == 0


async def test_explicit_batch_review_can_transition_bound_pending(ledger: _Session) -> None:
    pending = await _save()
    rows = await _persist([_decision()])
    assert rows[0].id == pending.id and rows[0].approval_status == "approved"
    original_review = rows[0].reviewed_at
    with pytest.raises(ValueError, match="Conflicting schema proposal retry"):
        await _save()
    assert next(iter(ledger.rows.values())).reviewed_at == original_review


async def test_approval_by_uuid_checks_binding_before_mutation(ledger: _Session) -> None:
    legacy = SchemaProposalRecord(
        id=uuid.uuid4(),
        tenant_id="plasticos",
        domain="plasticos",
        batch_run_id="old",
        field_name="metric",
        approval_status="pending",
    )
    ledger.rows[("plasticos", "plasticos", "old", None, "metric")] = legacy
    with pytest.raises(ValueError, match="legacy-unbound"):
        await pg_store.approve_schema_proposal(legacy.id, "reviewer", True)
    assert legacy.approval_status == "pending" and legacy.reviewed_at is None
    assert all(not str(s).lstrip().startswith("UPDATE") for s in ledger.statements)
    with pytest.raises(ValueError, match="Schema proposal not found"):
        await pg_store.approve_schema_proposal(uuid.uuid4(), "reviewer", True)


async def test_approval_by_uuid_is_idempotent_not_a_decision_override(ledger: _Session) -> None:
    row = await _save()
    approved = await pg_store.approve_schema_proposal(row.id, "reviewer", True)
    assert approved is row and row.approval_status == "approved"
    original = (row.reviewed_at, row.reviewed_by)
    await pg_store.approve_schema_proposal(row.id, "reviewer", True)
    assert (row.reviewed_at, row.reviewed_by) == original
    with pytest.raises(ValueError, match="Conflicting schema proposal retry"):
        await pg_store.approve_schema_proposal(row.id, "reviewer", False)


@pytest.mark.parametrize(
    "value",
    [
        SimpleNamespace(),
        {"node_label": None, "discovery_spec_sha256": None, "ownership_contract_version": None},
    ],
)
def test_binding_helper_stably_refuses_legacy(value: Any) -> None:
    with pytest.raises(ValueError, match="^Schema proposal is legacy-unbound$"):
        pg_store.validate_schema_proposal_binding(value)


def test_binding_helper_accepts_attribute_or_mapping_records() -> None:
    values = _decision()
    assert pg_store.validate_schema_proposal_binding(values) == ("Facility", _DIGEST)
    assert pg_store.validate_schema_proposal_binding(SimpleNamespace(**values)) == (
        "Facility",
        _DIGEST,
    )


async def test_reads_keep_legacy_history_for_consumer_refusal(ledger: _Session) -> None:
    legacy = SchemaProposalRecord(
        id=uuid.uuid4(),
        tenant_id="plasticos",
        domain="plasticos",
        batch_run_id="old",
        field_name="metric",
        approval_status="approved",
    )
    ledger.rows[("plasticos", "plasticos", "old", None, "metric")] = legacy
    assert await pg_store.get_approved_schema_proposals("plasticos", "plasticos") == [legacy]
    legacy.approval_status = "pending"
    assert await pg_store.get_pending_schema_proposals("plasticos", "plasticos") == [legacy]
    assert all("ownership_contract_version =" not in str(s) for s in ledger.statements)


async def test_numeric_database_scale_is_stable_for_retry(ledger: _Session) -> None:
    row = (await _persist([_decision(avg_confidence=0.91234)]))[0]
    row.avg_confidence = Decimal("0.9123")
    assert (await _persist([_decision(avg_confidence=0.91234)]))[0].id == row.id
