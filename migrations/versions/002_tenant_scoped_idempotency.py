"""Scope enrichment_results idempotency uniqueness to the tenant.

An idempotency key is chosen by the caller and is unique only inside that
caller's own tenant. The initial schema declared UNIQUE(idempotency_key)
globally, so two tenants using the same string collided: the lookup returned
the first tenant's stored enrichment to the second (a cross-tenant read whose
write was silently dropped and reported complete), and once the lookup was
tenant-scoped the insert failed outright on the global constraint.

Replaces the single-column UNIQUE with UNIQUE(tenant_id, idempotency_key), and
keeps a plain index on idempotency_key for lookups.

Revision ID: 002
"""

from __future__ import annotations

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None

_OLD_UNIQUE = "enrichment_results_idempotency_key_key"
_NEW_UNIQUE = "uq_enrichment_results_tenant_idempotency_key"
_KEY_INDEX = "ix_enrichment_results_idempotency_key"


def upgrade() -> None:
    # The single-column UNIQUE is created implicitly by `unique=True`, so its
    # name is PostgreSQL's default; IF EXISTS keeps the migration idempotent on
    # a database where it was already dropped.
    op.execute(f"ALTER TABLE enrichment_results DROP CONSTRAINT IF EXISTS {_OLD_UNIQUE}")

    # Dropping the constraint also drops the index it was backed by, so the
    # lookup index has to be recreated explicitly.
    op.execute(f"CREATE INDEX IF NOT EXISTS {_KEY_INDEX} ON enrichment_results (idempotency_key)")
    op.execute(
        f"ALTER TABLE enrichment_results ADD CONSTRAINT {_NEW_UNIQUE} "
        "UNIQUE (tenant_id, idempotency_key)"
    )


def downgrade() -> None:
    # Reversible only while no two tenants share a key; if they do, the old
    # global constraint cannot be recreated, which is the defect itself.
    op.execute(f"ALTER TABLE enrichment_results DROP CONSTRAINT IF EXISTS {_NEW_UNIQUE}")
    op.execute(f"DROP INDEX IF EXISTS {_KEY_INDEX}")
    op.execute(
        f"ALTER TABLE enrichment_results ADD CONSTRAINT {_OLD_UNIQUE} UNIQUE (idempotency_key)"
    )
