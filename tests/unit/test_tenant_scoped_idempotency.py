"""Idempotency is per tenant, in the query and in the schema.

`enrichment_results.idempotency_key` carried a global UNIQUE and was looked up
by key alone. An idempotency key is caller-chosen, so two tenants using the same
string is ordinary — and it produced a cross-tenant read: the second tenant's
converge resolved to the first tenant's stored enrichment and was reported
complete against a row it does not own, its own write silently dropped.

Static checks: they must hold on every runner, including those with no
PostgreSQL. The behavioural proof runs in the real-Postgres suite.
"""

from __future__ import annotations

import inspect
import os

os.environ.update(
    {
        "PERPLEXITY_API_KEY": "test-key",
        "API_SECRET_KEY": "test-secret-key-32-chars-long!!",
        "KB_DIR": "./kb",
    }
)

from sqlalchemy import UniqueConstraint

from app.services import pg_store
from app.services.pg_models import EnrichmentResult

_KEY = "idempotency_key"
_TENANT = "tenant_id"


def test_idempotency_key_is_not_globally_unique():
    column = EnrichmentResult.__table__.columns[_KEY]
    assert not column.unique, (
        "UNIQUE(idempotency_key) makes one tenant's caller-chosen key collide "
        "with another's; uniqueness belongs on (tenant_id, idempotency_key)"
    )


def test_unique_constraint_is_tenant_scoped():
    uniques = [c for c in EnrichmentResult.__table__.constraints if isinstance(c, UniqueConstraint)]
    scoped = [c for c in uniques if {col.name for col in c.columns} == {_TENANT, _KEY}]
    assert scoped, (
        "expected UNIQUE(tenant_id, idempotency_key) on enrichment_results, got "
        f"{[sorted(col.name for col in c.columns) for c in uniques]}"
    )

    for constraint in uniques:
        names = {col.name for col in constraint.columns}
        assert names != {_KEY}, "a global UNIQUE(idempotency_key) is still present"


def test_lookup_requires_a_tenant():
    """tenant_id is required, not an optional filter someone can forget."""
    sig = inspect.signature(pg_store.get_enrichment_result_by_idempotency_key)
    assert _TENANT in sig.parameters, "the lookup must take tenant_id"
    assert sig.parameters[_TENANT].default is inspect.Parameter.empty, (
        "tenant_id must be required; a default of None restores the cross-tenant read"
    )


def test_lookup_filters_on_both_columns():
    source = inspect.getsource(pg_store.get_enrichment_result_by_idempotency_key)
    assert "EnrichmentResult.tenant_id ==" in source, (
        "the query must filter on tenant_id, not only on idempotency_key"
    )
    assert f"EnrichmentResult.{_KEY} ==" in source


async def test_persist_scopes_its_replay_lookup_to_the_writing_tenant():
    """The replay lookup must be asked for the tenant the row is being written for."""
    from unittest.mock import AsyncMock, patch

    with patch.object(
        pg_store, "get_enrichment_result_by_idempotency_key", new_callable=AsyncMock
    ) as lookup:
        lookup.return_value = object()  # short-circuit before any real session
        await pg_store.save_enrichment_result(
            tenant_id="globex",
            entity_id="res.partner:1",
            object_type="res.partner",
            fields={},
            confidence=0.5,
            uncertainty_score=0.1,
            tokens_used=0,
            processing_time_ms=1,
            pass_count=1,
            idempotency_key="shared-key",
        )

    lookup.assert_awaited_once()
    assert lookup.await_args.kwargs.get("tenant_id") == "globex", (
        "save_enrichment_result must pass its own tenant into the replay lookup; "
        f"got {lookup.await_args!r}"
    )


def test_migration_replaces_the_global_constraint():
    """Load the migration module and check what it actually declares."""
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "002_tenant_scoped_idempotency.py"
    )
    spec = importlib.util.spec_from_file_location("_mig002", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "002"
    assert module.down_revision == "001", "must chain onto the initial schema"
    assert module._OLD_UNIQUE == "enrichment_results_idempotency_key_key"
    assert module._NEW_UNIQUE == "uq_enrichment_results_tenant_idempotency_key"

    # Record the SQL the migration issues, without a database.
    statements: list[str] = []
    original = module.op
    try:
        module.op = type("_Op", (), {"execute": staticmethod(statements.append)})()
        module.upgrade()
    finally:
        module.op = original

    sql = " ".join(statements)
    assert f"DROP CONSTRAINT IF EXISTS {module._OLD_UNIQUE}" in sql
    assert "UNIQUE (tenant_id, idempotency_key)" in sql
    assert "CREATE INDEX IF NOT EXISTS" in sql, (
        "dropping the constraint drops its backing index; the lookup index must be recreated"
    )
