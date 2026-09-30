#!/bin/sh
# DEV image entrypoint (Dockerfile only): apply the database schema, then run
# the image CMD. Dockerfile.prod does not use it — production applies the
# schema as an explicit, operator-run release step (AGENTS.md forbids
# `alembic upgrade head` in production context).
#
# `alembic upgrade head` is EIE's schema step (alembic.ini, migrations/). Without
# it a fresh database has no enrichment_results table and every durable
# `converge` fails with `relation "enrichment_results" does not exist`.
# Running it here is race-free because EIE runs exactly one replica with one
# worker (EIE-003 / EIE-212-F001). Skipped only when DATABASE_URL is unset, i.e.
# no database is configured for this container. A failed migration stops the
# container instead of serving against a stale schema.
set -e
if [ -n "${DATABASE_URL:-}" ]; then
  alembic upgrade head
fi
exec "$@"
