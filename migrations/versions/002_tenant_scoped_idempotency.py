"""Scope enrichment_results idempotency uniqueness to the tenant.

A caller-supplied idempotency key is not globally unique. Before this
migration ``enrichment_results.idempotency_key`` carried a table-wide UNIQUE
constraint, so the FIRST tenant to use a given string owned it across the whole
installation: a second tenant sending the same key had its write rejected, and
every lookup by that key alone could return the other tenant's row. The logical
operation is (tenant_id, idempotency_key); the constraint now says so.

Data safety
-----------
The new composite constraint is strictly WEAKER than the one it replaces: any
row set that satisfied a global UNIQUE on idempotency_key also satisfies a
UNIQUE on (tenant_id, idempotency_key). So this migration cannot fail on
existing data and destroys nothing — it only stops rejecting writes that were
always legitimate.

NULL keys stay unconstrained in both directions, because Postgres treats NULLs
as distinct in unique constraints. That is the intended semantics: a run whose
caller supplied no logical key is not a replay of anything.

Revision ID: 002
Revises: 001
"""

from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None

_NEW_CONSTRAINT = "uq_enrichment_results_tenant_idempotency"


def upgrade() -> None:
    # The old constraint was created implicitly by `unique=True` on the column,
    # so its name is server-generated (typically
    # `enrichment_results_idempotency_key_key`). Discover it rather than
    # guessing: dropping the wrong name, or hardcoding one this database never
    # used, would either fail the migration or silently leave the global
    # uniqueness in place — which is the defect being fixed.
    op.execute(
        """
        DO $$
        DECLARE
            conname_found text;
        BEGIN
            FOR conname_found IN
                SELECT con.conname
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
                WHERE rel.relname = 'enrichment_results'
                  AND nsp.nspname = current_schema()
                  AND con.contype = 'u'
                  AND (
                      SELECT array_agg(att.attname::text ORDER BY att.attname)
                      FROM unnest(con.conkey) AS k(attnum)
                      JOIN pg_attribute att
                        ON att.attrelid = con.conrelid AND att.attnum = k.attnum
                  ) = ARRAY['idempotency_key']
            LOOP
                EXECUTE format(
                    'ALTER TABLE enrichment_results DROP CONSTRAINT %I', conname_found
                );
            END LOOP;
        END $$;
        """
    )

    # A bare UNIQUE INDEX (rather than a constraint) on the same single column
    # would enforce the same global rule, so remove that shape too.
    op.execute(
        """
        DO $$
        DECLARE
            indexname_found text;
        BEGIN
            FOR indexname_found IN
                SELECT cls.relname
                FROM pg_index idx
                JOIN pg_class cls ON cls.oid = idx.indexrelid
                JOIN pg_class tbl ON tbl.oid = idx.indrelid
                JOIN pg_namespace nsp ON nsp.oid = tbl.relnamespace
                WHERE tbl.relname = 'enrichment_results'
                  AND nsp.nspname = current_schema()
                  AND idx.indisunique
                  AND NOT idx.indisprimary
                  AND NOT EXISTS (
                      SELECT 1 FROM pg_constraint con WHERE con.conindid = idx.indexrelid
                  )
                  AND (
                      SELECT array_agg(att.attname::text ORDER BY att.attname)
                      FROM unnest(idx.indkey) AS k(attnum)
                      JOIN pg_attribute att
                        ON att.attrelid = idx.indrelid AND att.attnum = k.attnum
                  ) = ARRAY['idempotency_key']
            LOOP
                EXECUTE format('DROP INDEX %I', indexname_found);
            END LOOP;
        END $$;
        """
    )

    op.create_unique_constraint(
        _NEW_CONSTRAINT,
        "enrichment_results",
        ["tenant_id", "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint(_NEW_CONSTRAINT, "enrichment_results", type_="unique")
    # Restoring the global constraint can legitimately fail: rows this
    # migration made possible (the same key under two tenants) violate it. That
    # failure is correct and must not be swallowed — silently deleting one
    # tenant's enrichment history to satisfy a downgrade would be data loss.
    op.create_unique_constraint(
        "enrichment_results_idempotency_key_key",
        "enrichment_results",
        ["idempotency_key"],
    )
