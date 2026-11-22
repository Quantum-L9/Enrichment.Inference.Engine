"""Graph-sync intents: the durable graph obligation of a committed result (A2).

One row per (tenant_id, effect_key) -- the semantic operation -- written in the
same transaction as the enrichment result it follows, so a result answered
`completed` never loses its graph leg to a restart. The row pins the exact
input a replay sends and carries the retry state and the last acknowledgment
or failure. Internal state only; no payload carries any of it.

Revision ID: 005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "graph_sync_intents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False, unique=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("effect_key", sa.String(512), nullable=False),
        sa.Column("entity_id", sa.String(256), nullable=False),
        sa.Column("node_label", sa.String(128), nullable=True),
        sa.Column("domain", sa.String(128), nullable=True),
        sa.Column("endpoint", sa.String(128), nullable=True),
        sa.Column("contract_digest", sa.String(64), nullable=True),
        sa.Column("cartridge_revision", sa.String(128), nullable=True),
        sa.Column("origin", sa.String(32), nullable=False, server_default="enrich"),
        sa.Column("fields", JSONB(), nullable=False, server_default="{}"),
        sa.Column("enrichment_result_id", UUID(as_uuid=True), nullable=True),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="8"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_ack", JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "effect_key", name="uq_graph_sync_intents_tenant_key"),
    )
    op.create_index("ix_graph_sync_intents_due", "graph_sync_intents", ["state", "next_attempt_at"])
    op.create_index(
        "ix_graph_sync_intents_subject",
        "graph_sync_intents",
        ["tenant_id", "entity_id", "endpoint", "seq"],
    )


def downgrade() -> None:
    op.drop_index("ix_graph_sync_intents_subject", table_name="graph_sync_intents")
    op.drop_index("ix_graph_sync_intents_due", table_name="graph_sync_intents")
    op.drop_table("graph_sync_intents")
