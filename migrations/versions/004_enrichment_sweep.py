"""Enrichment sweep: per-tenant daily budget ledger, sweeps and their results.

docs/contracts/enrichment-sweep.md. The ledger is what makes the daily budget
hold across sweep calls and process restarts: one row per (tenant_id, UTC day),
reserved with a conditional UPDATE so concurrent sweeps cannot pass the cap.

Revision ID: 004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sweep_budget_ledger",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("spent_usd", sa.Numeric(14, 6), nullable=False, server_default="0"),
        sa.Column("reserved_usd", sa.Numeric(14, 6), nullable=False, server_default="0"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("spent_usd >= 0", name="ck_sweep_ledger_spent_nonnegative"),
        sa.CheckConstraint("reserved_usd >= 0", name="ck_sweep_ledger_reserved_nonnegative"),
    )
    op.create_table(
        "enrichment_sweeps",
        sa.Column("sweep_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="running"),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("effective_budget_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("candidates", JSONB(), nullable=False, server_default="[]"),
        sa.Column("rejected", JSONB(), nullable=False, server_default="[]"),
        sa.Column("skipped", JSONB(), nullable=False, server_default="[]"),
        sa.Column("stats", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_enrichment_sweeps_tenant_key"),
        sa.CheckConstraint("state IN ('running', 'completed')", name="ck_enrichment_sweeps_state"),
    )
    op.create_index("ix_enrichment_sweeps_tenant_id", "enrichment_sweeps", ["tenant_id"])
    op.create_table(
        "enrichment_sweep_results",
        sa.Column(
            "sweep_id",
            sa.String(64),
            sa.ForeignKey("enrichment_sweeps.sweep_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("entity_id", sa.String(256), nullable=False),
        sa.Column("response", JSONB(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(14, 6), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("sweep_id", "entity_id", name="uq_enrichment_sweep_results_entity"),
    )


def downgrade() -> None:
    op.drop_table("enrichment_sweep_results")
    op.drop_index("ix_enrichment_sweeps_tenant_id", table_name="enrichment_sweeps")
    op.drop_table("enrichment_sweeps")
    op.drop_table("sweep_budget_ledger")
