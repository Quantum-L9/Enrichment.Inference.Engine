"""Persist discovery ownership without inventing historical audit provenance.

Revision ID: 003
Revises: 002

Artifact only: database rollout requires separate authorization, quiesced old
writers, backups, and PostgreSQL verification. Existing rows retain all three
binding columns NULL and retain every original review and timestamp.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None

_BINDING_CHECK = (
    "((node_label IS NULL AND discovery_spec_sha256 IS NULL "
    "AND ownership_contract_version IS NULL) OR "
    "(ownership_contract_version IS NOT NULL AND ownership_contract_version = 1 "
    "AND node_label IS NOT NULL AND char_length(node_label) <= 128 "
    "AND node_label ~ '^[A-Za-z_][A-Za-z0-9_]*$' "
    "AND discovery_spec_sha256 IS NOT NULL "
    "AND discovery_spec_sha256 ~ '^[0-9a-f]{64}$'))"
)


def upgrade() -> None:
    """Expand ownership in the migration transaction, preserving legacy NULLs."""
    op.execute("ALTER TABLE schema_proposals ADD COLUMN node_label VARCHAR(128) NULL")
    op.execute("ALTER TABLE schema_proposals ADD COLUMN discovery_spec_sha256 VARCHAR(64) NULL")
    op.execute("ALTER TABLE schema_proposals ADD COLUMN ownership_contract_version INTEGER NULL")
    op.execute(
        "ALTER TABLE schema_proposals ADD CONSTRAINT ck_schema_proposal_ownership_binding "
        f"CHECK ({_BINDING_CHECK})"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_schema_proposal_batch_node_field ON schema_proposals "
        "(tenant_id, domain, batch_run_id, node_label, field_name) "
        "WHERE ownership_contract_version = 1"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_schema_proposal_legacy_batch_field ON schema_proposals "
        "(tenant_id, domain, batch_run_id, field_name) "
        "WHERE ownership_contract_version IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_schema_proposals_owner_status_created ON schema_proposals "
        "(tenant_id, domain, node_label, approval_status, created_at)"
    )
    op.execute("ALTER TABLE schema_proposals DROP CONSTRAINT uq_schema_proposal_batch_field")
    # Install only after nullable expansion/checks, so existing rows stay NULL
    # while old writers without binding fail instead of adding unbound history.
    op.execute("ALTER TABLE schema_proposals ALTER COLUMN ownership_contract_version SET DEFAULT 1")


def downgrade() -> None:
    """Refuse provenance loss or old-key collisions BEFORE any schema DDL."""
    connection = op.get_bind()
    # Transaction-scoped lock is not DDL. It closes the preflight/write race;
    # no writer can add ownership after these checks and before column removal.
    connection.execute(text("LOCK TABLE schema_proposals IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM schema_proposals "
            "WHERE ownership_contract_version IS NOT NULL "
            "OR node_label IS NOT NULL OR discovery_spec_sha256 IS NOT NULL)"
        )
    ):
        raise RuntimeError(
            "Cannot downgrade schema proposal ownership: bound audit records would lose provenance"
        )
    if connection.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM schema_proposals "
            "GROUP BY tenant_id, domain, batch_run_id, field_name HAVING COUNT(*) > 1)"
        )
    ):
        raise RuntimeError(
            "Cannot downgrade schema proposal ownership: legacy audit records collide on the old key"
        )
    op.execute(
        "ALTER TABLE schema_proposals ADD CONSTRAINT uq_schema_proposal_batch_field "
        "UNIQUE (tenant_id, domain, batch_run_id, field_name)"
    )
    op.execute("DROP INDEX uq_schema_proposal_batch_node_field")
    op.execute("DROP INDEX uq_schema_proposal_legacy_batch_field")
    op.execute("DROP INDEX ix_schema_proposals_owner_status_created")
    op.execute("ALTER TABLE schema_proposals DROP CONSTRAINT ck_schema_proposal_ownership_binding")
    op.execute("ALTER TABLE schema_proposals DROP COLUMN ownership_contract_version")
    op.execute("ALTER TABLE schema_proposals DROP COLUMN discovery_spec_sha256")
    op.execute("ALTER TABLE schema_proposals DROP COLUMN node_label")
