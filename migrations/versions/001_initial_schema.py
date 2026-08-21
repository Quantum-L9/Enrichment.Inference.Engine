"""Initial schema: enrichment_results, convergence_runs,
field_confidence_history, schema_proposals.

Revision ID: 001

This is the only revision on the canonical `migrations/versions` tree.
It is the lineage root and current head. Do not import or depend on
`alembic/versions/0002_perplexity_api_key_default.py` (orphan, revises
a nonexistent 0001 and mutates config_snapshots).
"""

from __future__ import annotations

import importlib

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

op = importlib.import_module("alembic.op")

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "convergence_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("entity_id", sa.String(256), nullable=False),
        sa.Column("domain", sa.String(128), nullable=False),
        sa.Column("domain_yaml_version_before", sa.String(64), nullable=True),
        sa.Column("domain_yaml_version_after", sa.String(64), nullable=True),
        sa.Column("state", sa.String(32), nullable=False, server_default="running"),
        sa.Column("convergence_reason", sa.String(128), nullable=True),
        sa.Column("current_pass", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_passes", sa.Integer, nullable=False, server_default="5"),
        sa.Column("accumulated_fields", JSONB, nullable=False, server_default="{}"),
        sa.Column("accumulated_confidences", JSONB, nullable=False, server_default="{}"),
        sa.Column("pass_results", JSONB, nullable=False, server_default="[]"),
        sa.Column("total_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("max_budget_tokens", sa.Integer, nullable=False, server_default="50000"),
        sa.Column("schema_proposals", JSONB, nullable=False, server_default="[]"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_convergence_runs_tenant_entity", "convergence_runs", ["tenant_id", "entity_id"]
    )
    op.create_index("ix_convergence_runs_state", "convergence_runs", ["state"])

    op.create_table(
        "enrichment_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("entity_id", sa.String(256), nullable=False),
        sa.Column("object_type", sa.String(128), nullable=False),
        sa.Column("domain", sa.String(128), nullable=True),
        sa.Column("idempotency_key", sa.String(256), nullable=True),
        sa.Column("fields", JSONB, nullable=False, server_default="{}"),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False, server_default="0"),
        sa.Column("uncertainty_score", sa.Float, nullable=False, server_default="0"),
        sa.Column("quality_tier", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("state", sa.String(32), nullable=False, server_default="completed"),
        sa.Column("failure_reason", sa.Text, nullable=True),
        sa.Column("tokens_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("processing_time_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("pass_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("inferences", JSONB, nullable=False, server_default="[]"),
        sa.Column("kb_fragment_ids", JSONB, nullable=False, server_default="[]"),
        sa.Column("feature_vector", JSONB, nullable=True),
        sa.Column(
            "convergence_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("convergence_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_enrichment_results_tenant_entity", "enrichment_results", ["tenant_id", "entity_id"]
    )
    op.create_index(
        "ix_enrichment_results_tenant_created", "enrichment_results", ["tenant_id", "created_at"]
    )
    op.create_unique_constraint(
        "uq_enrichment_results_tenant_idempotency",
        "enrichment_results",
        ["tenant_id", "idempotency_key"],
    )
    op.create_index(
        "ix_enrichment_results_convergence_run", "enrichment_results", ["convergence_run_id"]
    )

    op.create_table(
        "field_confidence_history",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "enrichment_result_id",
            UUID(as_uuid=True),
            sa.ForeignKey("enrichment_results.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("entity_id", sa.String(256), nullable=False),
        sa.Column("field_name", sa.String(256), nullable=False),
        sa.Column("field_value", sa.Text, nullable=True),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("pass_number", sa.Integer, nullable=False),
        sa.Column("variation_agreement", sa.Numeric(5, 4), nullable=True),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_field_confidence_entity_field",
        "field_confidence_history",
        ["tenant_id", "entity_id", "field_name"],
    )
    op.create_index(
        "ix_field_confidence_result_pass",
        "field_confidence_history",
        ["enrichment_result_id", "pass_number"],
    )

    op.create_table(
        "schema_proposals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("domain", sa.String(128), nullable=False),
        sa.Column("batch_run_id", sa.String(256), nullable=False),
        sa.Column("field_name", sa.String(256), nullable=False),
        sa.Column("field_type", sa.String(64), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("fill_rate", sa.Numeric(5, 4), nullable=False),
        sa.Column("avg_confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("sample_values", JSONB, nullable=False, server_default="[]"),
        sa.Column("value_distribution", JSONB, nullable=False, server_default="{}"),
        sa.Column("proposed_gate", sa.Text, nullable=True),
        sa.Column("proposed_scoring_dimension", sa.Text, nullable=True),
        sa.Column("yaml_diff", sa.Text, nullable=True),
        sa.Column("approval_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("reviewed_by", sa.String(256), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "domain",
            "batch_run_id",
            "field_name",
            name="uq_schema_proposal_batch_field",
        ),
    )
    op.create_index(
        "ix_schema_proposals_domain_status", "schema_proposals", ["domain", "approval_status"]
    )
    op.create_index("ix_schema_proposals_tenant", "schema_proposals", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("schema_proposals")
    op.drop_table("field_confidence_history")
    op.drop_table("enrichment_results")
    op.drop_table("convergence_runs")
