#!/bin/sh
# Container entrypoint: apply the database schema, then run the image CMD.
#
# `alembic upgrade head` is EIE's schema step (alembic.ini, migrations/). Without
# it a fresh database has no enrichment_results table and every durable
# `converge` fails with `relation "enrichment_results" does not exist`.
# Running it here is race-free because EIE deploys exactly one replica with one
# worker (EIE-003 / EIE-212-F001). Skipped only when DATABASE_URL is unset, i.e.
# no database is configured for this container. A failed migration stops the
# container instead of serving against a stale schema.
set -e
if [ -n "${DATABASE_URL:-}" ]; then
  alembic upgrade head
fi
exec "$@"
