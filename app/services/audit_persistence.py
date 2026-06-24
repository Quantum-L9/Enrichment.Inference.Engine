"""
Audit log persistence for compliance tracking.

Provides PostgreSQL-backed audit logging for all enrichment operations.
Call configure_audit_pool() at app startup after creating the asyncpg pool.

Usage:
    # At startup
    pool = await asyncpg.create_pool(dsn)
    await configure_audit_pool(pool)

    # During operation
    await flush_audit_entries([
        {"tenant_id": "acme", "actor": "system", "action": "enrich", "detail": "..."}
    ])
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_POOL = None  # asyncpg.Pool, set by configure_audit_pool

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT             NOT NULL,
    actor       TEXT             NOT NULL,
    action      TEXT             NOT NULL,
    detail      TEXT,
    created_at  DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_created
    ON audit_log (tenant_id, created_at DESC);
"""


async def configure_audit_pool(pool) -> None:
    """
    Configure the audit persistence pool.
    Call once at startup after asyncpg.create_pool().

    Args:
        pool: asyncpg.Pool instance
    """
    global _POOL
    _POOL = pool
    async with pool.acquire() as conn:
        await conn.execute(_CREATE_TABLE_SQL)
    logger.info("audit_persistence: PostgreSQL pool configured and schema verified")


async def flush_audit_entries(entries: list[dict[str, Any]]) -> int:
    """
    Persist audit entries to PostgreSQL.
    Returns count of rows inserted.

    Args:
        entries: List of audit entry dicts with keys:
            - tenant_id: str
            - actor: str (default: "system")
            - action: str
            - detail: str (optional)
            - created_at: float (default: current time)
    """
    if _POOL is None:
        logger.error(
            "flush_audit_entries called but db_pool is None — "
            "call configure_audit_pool() at startup. Entries dropped: %d",
            len(entries),
        )
        return 0
    if not entries:
        return 0

    rows = [
        (
            e.get("tenant_id", "unknown"),
            e.get("actor", "system"),
            e.get("action", "unknown"),
            e.get("detail"),
            e.get("created_at", time.time()),
        )
        for e in entries
    ]
    async with _POOL.acquire() as conn:
        await conn.executemany(
            "INSERT INTO audit_log (tenant_id, actor, action, detail, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            rows,
        )
    logger.debug("audit_persistence: flushed %d entries to PostgreSQL", len(rows))
    return len(rows)


async def query_audit_log(
    tenant_id: str,
    *,
    action: str | None = None,
    since: float | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Query audit log entries for a tenant.

    Args:
        tenant_id: Tenant to query
        action: Filter by action type (optional)
        since: Unix timestamp to filter entries after (optional)
        limit: Maximum entries to return

    Returns:
        List of audit entry dicts
    """
    if _POOL is None:
        logger.error("query_audit_log called but db_pool is None")
        return []

    query = "SELECT id, tenant_id, actor, action, detail, created_at FROM audit_log WHERE tenant_id = $1"
    params: list[Any] = [tenant_id]

    if action:
        query += " AND action = $2"
        params.append(action)

    if since:
        query += f" AND created_at >= ${len(params) + 1}"
        params.append(since)

    query += f" ORDER BY created_at DESC LIMIT ${len(params) + 1}"
    params.append(limit)

    async with _POOL.acquire() as conn:
        rows = await conn.fetch(query, *params)

    return [
        {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "actor": row["actor"],
            "action": row["action"],
            "detail": row["detail"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
