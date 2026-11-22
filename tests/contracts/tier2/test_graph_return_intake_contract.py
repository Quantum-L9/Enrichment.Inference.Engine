"""Tier 2: `graph_return_targets` matches its data contract.

Contract: docs/contracts/data/graph_return_intake.md. The model, the migration
and the documented columns, constraints, indexes, states, reasons and bounds
must agree; no column may leak into a public payload, and the intake answer
keeps its existing keys.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import UniqueConstraint

from app.services import graph_return_channel as grc
from app.services import pg_store
from app.services.pg_store import GraphReturnTarget

REPO = Path(__file__).resolve().parents[3]
DOC = REPO / "docs" / "contracts" / "data" / "graph_return_intake.md"
MIGRATION = REPO / "migrations" / "versions" / "006_graph_return_targets.py"

COLUMNS = {
    "id",
    "seq",
    "tenant_id",
    "entity_id",
    "field_name",
    "rule",
    "value",
    "value_digest",
    "fact_key",
    "intake_key",
    "producer_key",
    "origin_packet_id",
    "confidence",
    "state",
    "reason",
    "outcome",
    "attempts",
    "max_attempts",
    "lease_owner",
    "lease_expires_at",
    "expires_at",
    "consumed_by",
    "created_at",
    "updated_at",
    "resolved_at",
}


def test_model_columns_are_the_documented_columns() -> None:
    assert set(GraphReturnTarget.__table__.columns.keys()) == COLUMNS
    doc = DOC.read_text(encoding="utf-8")
    for column in COLUMNS:
        assert f"`{column}`" in doc, column


def test_migration_creates_every_model_column_after_005() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert re.search(r'^down_revision = "005"$', source, re.M)
    assert re.search(r'^revision = "006"$', source, re.M)
    created = set(re.findall(r'sa\.Column\(\s*"([a-z_]+)"', source))
    assert created == COLUMNS


def test_intake_key_is_unique_per_tenant() -> None:
    uniques = {
        c.name: tuple(col.name for col in c.columns)
        for c in GraphReturnTarget.__table__.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert uniques["uq_graph_return_targets_tenant_intake"] == ("tenant_id", "intake_key")
    assert "uq_graph_return_targets_tenant_intake" in MIGRATION.read_text(encoding="utf-8")


def test_entity_and_fact_indexes_match_the_migration() -> None:
    indexes = {
        i.name: tuple(c.name for c in i.columns) for i in GraphReturnTarget.__table__.indexes
    }
    assert indexes == {
        "ix_graph_return_targets_entity": ("tenant_id", "entity_id", "state", "seq"),
        "ix_graph_return_targets_fact": ("tenant_id", "fact_key"),
    }
    source = MIGRATION.read_text(encoding="utf-8")
    for name in indexes:
        assert name in source


def test_states_reasons_and_outcomes_are_documented() -> None:
    states = {
        pg_store.GRAPH_RETURN_RETAINED,
        pg_store.GRAPH_RETURN_CONSUMING,
        pg_store.GRAPH_RETURN_CONSUMED,
        pg_store.GRAPH_RETURN_REJECTED,
        pg_store.GRAPH_RETURN_EXPIRED,
    }
    assert states == {"retained", "consuming", "consumed", "rejected", "expired"}
    assert {"consumed", "rejected", "expired"} == pg_store.GRAPH_RETURN_TERMINAL_STATES
    doc = DOC.read_text(encoding="utf-8")
    words = states | {
        grc.REJECT_MALFORMED,
        grc.REJECT_CROSS_TENANT,
        grc.REJECT_BELOW_THRESHOLD,
        grc.REJECT_FIELD_NOT_IN_SCHEMA,
        "unchanged_fact",
        "ttl_expired",
        "attempts_exhausted",
        "seeded",
        "outranked",
        "kept_entity_value",
        "kept_stronger_value",
    }
    for word in words:
        assert f"`{word}`" in doc, word


def test_documented_bounds_are_the_code_bounds() -> None:
    doc = DOC.read_text(encoding="utf-8")
    assert grc.GRAPH_RETURN_TENANT_CAPACITY == 10_000 and "(10 000)" in doc
    assert grc.GRAPH_RETURN_LEASE_SECONDS == 300 and "(300 s)" in doc
    assert grc.GRAPH_RETURN_TTL_SECONDS == 7 * 24 * 3600 and "(7 days)" in doc
    assert pg_store.GRAPH_RETURN_DEFAULT_MAX_ATTEMPTS == 5 and "(default 5)" in doc
    assert grc.CONFIDENCE_FLOOR == 0.55 and "(0.55)" in doc


def test_no_intake_column_reaches_a_public_payload_contract() -> None:
    """Internal state only: no payload schema under docs/contracts names an intake column."""
    internal = {"intake_key", "fact_key", "producer_key", "lease_owner", "consumed_by"}
    for schema in (REPO / "docs" / "contracts").rglob("*.json"):
        text = schema.read_text(encoding="utf-8")
        for column in internal:
            assert f'"{column}"' not in text, (schema, column)
