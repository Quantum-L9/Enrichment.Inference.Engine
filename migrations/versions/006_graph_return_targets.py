"""Graph-return targets: CEG feedback retained until a converge consumes it (A4).

One row per (tenant_id, intake_key) -- the producer's idempotency key plus the
fact (tenant, entity, field, rule, value) -- recorded in one transaction per
graph-inference-result packet before the packet is acknowledged, so a restart
or a second worker never loses or doubles a target. A later converge of the
row's tenant and entity leases it and records what it did with it. Internal
state only; no payload carries any of it.

Revision ID: 006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "graph_return_targets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False, unique=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("entity_id", sa.String(256), nullable=False),
        sa.Column("field_name", sa.String(128), nullable=False),
        sa.Column("rule", sa.String(128), nullable=False),
        sa.Column("value", JSONB(), nullable=False),
        sa.Column("value_digest", sa.String(64), nullable=False),
        sa.Column("fact_key", sa.String(64), nullable=False),
        sa.Column("intake_key", sa.String(64), nullable=False),
        sa.Column("producer_key", sa.String(256), nullable=False),
        sa.Column("origin_packet_id", sa.String(128), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="retained"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("outcome", sa.String(64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_by", sa.String(512), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "intake_key", name="uq_graph_return_targets_tenant_intake"
        ),
    )
    op.create_index(
        "ix_graph_return_targets_entity",
        "graph_return_targets",
        ["tenant_id", "entity_id", "state", "seq"],
    )
    op.create_index(
        "ix_graph_return_targets_fact", "graph_return_targets", ["tenant_id", "fact_key"]
    )


def downgrade() -> None:
    op.drop_index("ix_graph_return_targets_fact", table_name="graph_return_targets")
    op.drop_index("ix_graph_return_targets_entity", table_name="graph_return_targets")
    op.drop_table("graph_return_targets")
