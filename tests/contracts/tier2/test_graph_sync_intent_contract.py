"""Tier 2: `graph_sync_intents` matches its data contract.

Contract: docs/contracts/data/graph_sync_intents.md. The model, the migration
and the documented columns, constraints, indexes and states must agree; no
column may leak into a public payload.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import UniqueConstraint

from app.services import pg_store
from app.services.pg_store import GraphSyncIntent

REPO = Path(__file__).resolve().parents[3]
DOC = REPO / "docs" / "contracts" / "data" / "graph_sync_intents.md"
MIGRATION = REPO / "migrations" / "versions" / "005_graph_sync_intents.py"

COLUMNS = {
    "id",
    "seq",
    "tenant_id",
    "effect_key",
    "entity_id",
    "node_label",
    "domain",
    "endpoint",
    "contract_digest",
    "cartridge_revision",
    "fields",
    "enrichment_result_id",
    "state",
    "attempts",
    "max_attempts",
    "next_attempt_at",
    "lease_owner",
    "lease_expires_at",
    "last_error",
    "last_ack",
    "created_at",
    "updated_at",
    "resolved_at",
}


def test_model_columns_are_the_documented_columns() -> None:
    assert set(GraphSyncIntent.__table__.columns.keys()) == COLUMNS
    doc = DOC.read_text(encoding="utf-8")
    for column in COLUMNS:
        assert f"`{column}`" in doc, column


def test_migration_creates_every_model_column_after_004() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert re.search(r'^down_revision = "004"$', source, re.M)
    assert re.search(r'^revision = "005"$', source, re.M)
    created = set(re.findall(r'sa\.Column\(\s*"([a-z_]+)"', source))
    assert created == COLUMNS


def test_operation_key_is_unique_per_tenant() -> None:
    uniques = {
        c.name: tuple(col.name for col in c.columns)
        for c in GraphSyncIntent.__table__.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert uniques["uq_graph_sync_intents_tenant_key"] == ("tenant_id", "effect_key")
    assert "uq_graph_sync_intents_tenant_key" in MIGRATION.read_text(encoding="utf-8")


def test_due_and_subject_indexes_match_the_migration() -> None:
    indexes = {i.name: tuple(c.name for c in i.columns) for i in GraphSyncIntent.__table__.indexes}
    assert indexes["ix_graph_sync_intents_due"] == ("state", "next_attempt_at")
    assert indexes["ix_graph_sync_intents_subject"] == (
        "tenant_id",
        "entity_id",
        "node_label",
        "seq",
    )
    source = MIGRATION.read_text(encoding="utf-8")
    for name in indexes:
        assert name in source


def test_states_are_the_documented_states() -> None:
    states = {
        pg_store.GRAPH_INTENT_PENDING,
        pg_store.GRAPH_INTENT_DISPATCHING,
        pg_store.GRAPH_INTENT_ACKNOWLEDGED,
        pg_store.GRAPH_INTENT_FAILED_TERMINAL,
        pg_store.GRAPH_INTENT_SUPERSEDED,
    }
    assert states == {"pending", "dispatching", "acknowledged", "failed_terminal", "superseded"}
    assert {
        "acknowledged",
        "failed_terminal",
        "superseded",
    } == pg_store.GRAPH_INTENT_TERMINAL_STATES
    doc = DOC.read_text(encoding="utf-8")
    for state in states:
        assert f"`{state}`" in doc, state


def test_no_intent_column_reaches_a_public_payload_contract() -> None:
    """Internal state only: no payload schema under docs/contracts names an intent column."""
    internal = {"effect_key", "lease_owner", "lease_expires_at", "next_attempt_at"}
    for schema in (REPO / "docs" / "contracts").rglob("*.json"):
        text = schema.read_text(encoding="utf-8")
        for column in internal:
            assert f'"{column}"' not in text, (schema, column)
