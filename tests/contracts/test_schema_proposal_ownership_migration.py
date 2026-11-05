"""Unexecuted migration/ORM contracts. No test opens a database connection."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[2]
_PATH = _ROOT / "migrations/versions/003_schema_proposal_node_ownership.py"


def _source() -> str:
    assert _PATH.is_file(), "Option A requires the authorized, unexecuted 003 migration"
    return _PATH.read_text(encoding="utf-8")


class _Operations:
    """Capture constant SQL and simulate downgrade preflight scalar results."""

    def __init__(self, bound: bool = False, collision: bool = False) -> None:
        self.statements: list[str] = []
        self.queries: list[str] = []
        self.locks: list[str] = []
        self.bound = bound
        self.collision = collision

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))

    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(
            scalar=self.scalar, execute=lambda statement: self.locks.append(str(statement))
        )

    def scalar(self, statement: Any) -> bool:
        text = str(statement)
        self.queries.append(text)
        if "ownership_contract_version" in text:
            return self.bound
        assert "GROUP BY tenant_id, domain, batch_run_id, field_name" in text
        assert "HAVING COUNT(*) > 1" in text
        return self.collision


def _migration(monkeypatch: pytest.MonkeyPatch, operations: _Operations) -> ModuleType:
    _source()
    alembic = ModuleType("alembic")
    alembic.op = operations
    monkeypatch.setitem(sys.modules, "alembic", alembic)
    spec = importlib.util.spec_from_file_location("ownership_migration_contract", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lineage_is_settled_002_then_003() -> None:
    tree = ast.parse(_source())
    assignments = {
        n.targets[0].id: ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, ast.Assign)
        and isinstance(n.targets[0], ast.Name)
        and isinstance(n.value, ast.Constant)
    }
    assert assignments["revision"] == "003"
    assert assignments["down_revision"] == "002"
    predecessor = ast.parse(
        (_ROOT / "migrations/versions/002_tenant_scoped_idempotency.py").read_text(encoding="utf-8")
    )
    assert any(
        isinstance(n, ast.Assign)
        and isinstance(n.targets[0], ast.Name)
        and n.targets[0].id == "revision"
        and isinstance(n.value, ast.Constant)
        and n.value.value == "002"
        for n in predecessor.body
    )


def test_expand_retains_legacy_nulls_and_default_is_installed_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations = _Operations()
    _migration(monkeypatch, operations).upgrade()
    sql = operations.statements
    additions = [s for s in sql if "ADD COLUMN" in s]
    assert len(additions) == 3
    assert any("node_label VARCHAR(128) NULL" in s for s in additions)
    assert any("discovery_spec_sha256 VARCHAR(64) NULL" in s for s in additions)
    assert any("ownership_contract_version INTEGER NULL" in s for s in additions)
    assert all("DEFAULT" not in s for s in additions)
    assert (
        "ALTER TABLE schema_proposals ALTER COLUMN ownership_contract_version SET DEFAULT 1"
        in sql[-1]
    )
    assert not any("UPDATE schema_proposals" in s or "DELETE" in s for s in sql)
    assert all("reviewed_at" not in s and "reviewed_by" not in s for s in sql)
    assert any("DROP CONSTRAINT uq_schema_proposal_batch_field" in s for s in sql)
    bound = next(s for s in sql if "CREATE UNIQUE INDEX uq_schema_proposal_batch_node_field" in s)
    assert "(tenant_id, domain, batch_run_id, node_label, field_name)" in bound
    assert "WHERE ownership_contract_version = 1" in bound
    legacy = next(
        s for s in sql if "CREATE UNIQUE INDEX uq_schema_proposal_legacy_batch_field" in s
    )
    assert "(tenant_id, domain, batch_run_id, field_name)" in legacy
    assert "WHERE ownership_contract_version IS NULL" in legacy
    assert any("(tenant_id, domain, node_label, approval_status, created_at)" in s for s in sql)


def _assert_check(check: str) -> None:
    for field in ("node_label", "discovery_spec_sha256", "ownership_contract_version"):
        assert f"{field} IS NULL" in check
        assert f"{field} IS NOT NULL" in check
    assert "ownership_contract_version = 1" in check
    assert "char_length(node_label) <= 128" in check
    assert "node_label ~ '^[A-Za-z_][A-Za-z0-9_]*$'" in check
    assert "discovery_spec_sha256 ~ '^[0-9a-f]{64}$'" in check
    assert " OR " in check


def test_check_has_explicit_null_branches_matching_orm(monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import CheckConstraint, UniqueConstraint

    from app.services.pg_models import SchemaProposalRecord

    operations = _Operations()
    _migration(monkeypatch, operations).upgrade()
    migration_check = next(
        s
        for s in operations.statements
        if "ADD CONSTRAINT ck_schema_proposal_ownership_binding" in s
    )
    _assert_check(migration_check)
    table = SchemaProposalRecord.__table__
    orm_check = next(
        c
        for c in table.constraints
        if isinstance(c, CheckConstraint) and c.name == "ck_schema_proposal_ownership_binding"
    )
    _assert_check(str(orm_check.sqltext))
    assert str(orm_check.sqltext) in migration_check
    assert not any(
        isinstance(c, UniqueConstraint) and c.name == "uq_schema_proposal_batch_field"
        for c in table.constraints
    )
    for name, length in (("node_label", 128), ("discovery_spec_sha256", 64)):
        column = table.columns[name]
        assert column.nullable and column.type.length == length
        assert column.server_default is None
    version = table.columns["ownership_contract_version"]
    assert version.nullable and str(version.server_default.arg) == "1"
    indexes = {index.name: index for index in table.indexes}
    for name, columns, predicate in (
        (
            "uq_schema_proposal_batch_node_field",
            ["tenant_id", "domain", "batch_run_id", "node_label", "field_name"],
            "ownership_contract_version = 1",
        ),
        (
            "uq_schema_proposal_legacy_batch_field",
            ["tenant_id", "domain", "batch_run_id", "field_name"],
            "ownership_contract_version IS NULL",
        ),
    ):
        assert indexes[name].unique
        assert [c.name for c in indexes[name].columns] == columns
        assert str(indexes[name].dialect_options["postgresql"]["where"]) == predicate
    assert [c.name for c in indexes["ix_schema_proposals_owner_status_created"].columns] == [
        "tenant_id",
        "domain",
        "node_label",
        "approval_status",
        "created_at",
    ]


@pytest.mark.parametrize("bound,collision", [(True, False), (False, True), (True, True)])
def test_lossy_downgrade_refuses_before_any_ddl(
    monkeypatch: pytest.MonkeyPatch, bound: bool, collision: bool
) -> None:
    operations = _Operations(bound, collision)
    module = _migration(monkeypatch, operations)
    with pytest.raises(RuntimeError, match="Cannot downgrade schema proposal ownership"):
        module.downgrade()
    assert operations.statements == []
    assert operations.locks == ["LOCK TABLE schema_proposals IN ACCESS EXCLUSIVE MODE"]
    assert operations.queries


def test_lossless_downgrade_restores_old_key_without_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations = _Operations()
    _migration(monkeypatch, operations).downgrade()
    assert len(operations.queries) == 2
    assert operations.locks == ["LOCK TABLE schema_proposals IN ACCESS EXCLUSIVE MODE"]
    sql = " ".join(operations.statements)
    assert (
        "ADD CONSTRAINT uq_schema_proposal_batch_field UNIQUE (tenant_id, domain, batch_run_id, field_name)"
        in sql
    )
    for field in ("node_label", "discovery_spec_sha256", "ownership_contract_version"):
        assert f"DROP COLUMN {field}" in sql
    assert "DELETE" not in sql and "UPDATE" not in sql


def test_migration_is_only_constant_ddl_and_read_only_preflights() -> None:
    tree = ast.parse(_source())
    assert not any(
        isinstance(n, ast.Import)
        and any(a.name in {"asyncpg", "psycopg", "subprocess"} for a in n.names)
        for n in ast.walk(tree)
    )
    assert not any(
        isinstance(n, ast.Attribute) and n.attr in {"create_engine", "connect"}
        for n in ast.walk(tree)
    )
    assert "op.get_bind()" in _source()
