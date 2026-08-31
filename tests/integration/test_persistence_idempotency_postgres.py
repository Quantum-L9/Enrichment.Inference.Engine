"""Real-PostgreSQL gates for the enrichment persistence boundary.

Every assertion here needs a real server. Tenant-scoped uniqueness, NULL-key
semantics, transactional rollback and a concurrent unique-key race are all
behaviours of PostgreSQL, not of SQLAlchemy: a SQLite or mocked-session version
of this file would pass while the production database did the opposite. That is
exactly how the defects these tests pin survived — the old global UNIQUE on
`idempotency_key` was visible only against a real server holding rows from more
than one tenant.

Set `EIE_TEST_DATABASE_URL` to an asyncpg URL for a database this suite may
create and drop tables in. Without it the module skips: an unavailable server
is an environment fact, not a passing gate, and the skip reason says which
proof was not obtained.

L9_META:
  tier: 2
  domain: persistence
  authority: L9 Master Kernel v3.0
  pr_class: app_code + tier2_test
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.models.schemas import EnrichResponse
from app.services import pg_store
from app.services.pg_models import Base, EnrichmentResult
from app.services.result_store import ResultStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

DATABASE_URL = os.getenv("EIE_TEST_DATABASE_URL", "").strip()

pytest_skip_reason = (
    "EIE_TEST_DATABASE_URL is unset — real-PostgreSQL persistence, tenant "
    "isolation and unique-race proofs were NOT obtained"
)


@pytest_asyncio.fixture
async def pg() -> Any:
    """A real engine against a real server, with a clean table per test."""
    if not DATABASE_URL:
        pytest.skip(pytest_skip_reason)

    pg_store.init_engine(DATABASE_URL, pool_size=5, max_overflow=10)
    engine = pg_store._engine
    assert engine is not None

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    try:
        yield engine
    finally:
        await pg_store.close_engine()


def _response(**overrides: Any) -> EnrichResponse:
    payload: dict[str, Any] = {
        "fields": {"website": "https://acme.example"},
        "confidence": 0.9,
        "uncertainty_score": 0.1,
        "pass_count": 1,
        "state": "completed",
    }
    payload.update(overrides)
    return EnrichResponse(**payload)


async def _rows(engine, **where: str) -> list[EnrichmentResult]:
    """Read through an INDEPENDENT session, so nothing unflushed is counted."""
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(engine) as session:
        stmt = select(EnrichmentResult)
        for column, value in where.items():
            stmt = stmt.where(getattr(EnrichmentResult, column) == value)
        result = await session.execute(stmt)
        return list(result.scalars().all())


# ── Durable-before-completed ───────────────────────────────────────────────


async def test_successful_persistence_is_visible_to_an_independent_session(pg) -> None:
    """ADR-EIE-006 — "persisted" means a committed row another session can read."""
    store = ResultStore(tenant_id="acme")
    key = f"converge:{uuid.uuid4()}"

    await store.persist_enrich_response(
        response=_response(),
        entity_id="res.partner:55",
        object_type="plasticos",
        domain="plasticos",
        idempotency_key=key,
    )

    rows = await _rows(pg, tenant_id="acme", idempotency_key=key)
    assert len(rows) == 1
    assert rows[0].fields == {"website": "https://acme.example"}
    assert rows[0].state == "completed"


async def test_failed_transaction_leaves_no_row(pg) -> None:
    """A rolled-back write must leave nothing durable behind.

    The failure is injected at the database, not at the ORM: an oversized
    `object_type` violates the column width, so PostgreSQL itself aborts the
    transaction. That proves the rollback path rather than a Python guard.
    """
    key = f"converge:{uuid.uuid4()}"
    store = ResultStore(tenant_id="acme")

    with pytest.raises(DBAPIError):
        await store.persist_enrich_response(
            response=_response(),
            entity_id="res.partner:56",
            object_type="x" * 500,  # column is String(128)
            domain="plasticos",
            idempotency_key=key,
        )

    assert await _rows(pg, idempotency_key=key) == []


# ── Tenant isolation ───────────────────────────────────────────────────────


async def test_same_raw_key_under_two_tenants_creates_two_operations(pg) -> None:
    """ADR-EIE-005 — a caller key is unique within a tenant, not globally.

    Under the previous global UNIQUE constraint the second write here raised
    and the second tenant's enrichment was lost.
    """
    shared_key = f"converge:{uuid.uuid4()}"

    for tenant, site in (("tenant-a", "https://a.example"), ("tenant-b", "https://b.example")):
        await ResultStore(tenant_id=tenant).persist_enrich_response(
            response=_response(fields={"website": site}),
            entity_id="res.partner:55",
            object_type="plasticos",
            domain="plasticos",
            idempotency_key=shared_key,
        )

    rows = await _rows(pg, idempotency_key=shared_key)
    assert len(rows) == 2
    assert {r.tenant_id for r in rows} == {"tenant-a", "tenant-b"}
    assert {r.fields["website"] for r in rows} == {"https://a.example", "https://b.example"}


async def test_lookup_never_returns_another_tenants_row(pg) -> None:
    """The lookup is keyed by (tenant, key), so it cannot cross the boundary."""
    shared_key = f"converge:{uuid.uuid4()}"
    await ResultStore(tenant_id="tenant-a").persist_enrich_response(
        response=_response(fields={"website": "https://a.example"}),
        entity_id="res.partner:55",
        object_type="plasticos",
        idempotency_key=shared_key,
    )

    seen_by_b = await pg_store.get_enrichment_result_by_idempotency_key("tenant-b", shared_key)
    assert seen_by_b is None

    seen_by_a = await pg_store.get_enrichment_result_by_idempotency_key("tenant-a", shared_key)
    assert seen_by_a is not None
    assert seen_by_a.tenant_id == "tenant-a"


# ── Replay and concurrency ─────────────────────────────────────────────────


async def test_same_logical_key_replay_returns_the_same_row(pg) -> None:
    """ADR-EIE-008 — one logical operation, one durable result."""
    key = f"converge:{uuid.uuid4()}"
    store = ResultStore(tenant_id="acme")

    first = await store.persist_enrich_response(
        response=_response(),
        entity_id="res.partner:55",
        object_type="plasticos",
        idempotency_key=key,
    )
    second = await store.persist_enrich_response(
        response=_response(fields={"website": "https://changed.example"}),
        entity_id="res.partner:55",
        object_type="plasticos",
        idempotency_key=key,
    )

    assert first == second
    assert len(await _rows(pg, tenant_id="acme", idempotency_key=key)) == 1


async def test_new_logical_run_for_the_same_entity_is_a_new_operation(pg) -> None:
    """The entity is not the identity — a second genuine run must persist."""
    store = ResultStore(tenant_id="acme")
    for _ in range(2):
        await store.persist_enrich_response(
            response=_response(),
            entity_id="res.partner:77",
            object_type="plasticos",
            idempotency_key=f"converge:{uuid.uuid4()}",
        )

    assert len(await _rows(pg, tenant_id="acme", entity_id="res.partner:77")) == 2


async def test_rows_without_a_logical_key_are_not_deduplicated(pg) -> None:
    """NULL keys stay distinct: no key means "not a replay of anything"."""
    store = ResultStore(tenant_id="acme")
    for _ in range(3):
        await store.persist_enrich_response(
            response=_response(),
            entity_id="res.partner:88",
            object_type="plasticos",
            idempotency_key=None,
        )

    assert len(await _rows(pg, tenant_id="acme", entity_id="res.partner:88")) == 3


async def test_concurrent_same_key_writers_produce_exactly_one_row(pg) -> None:
    """ADR-EIE-007 — the database, not a process-local set, resolves the race.

    Both coroutines are released together and both miss the pre-read, so both
    attempt the INSERT. PostgreSQL rejects one; that loser must resolve into an
    idempotent hit on the winner's row rather than surfacing as a persistence
    failure — a duplicate of an operation that already committed is a success
    for the caller.
    """
    key = f"converge:{uuid.uuid4()}"
    store = ResultStore(tenant_id="acme")
    start = asyncio.Event()

    async def writer(site: str) -> uuid.UUID:
        await start.wait()
        return await store.persist_enrich_response(
            response=_response(fields={"website": site}),
            entity_id="res.partner:99",
            object_type="plasticos",
            idempotency_key=key,
        )

    tasks = [
        asyncio.create_task(writer("https://one.example")),
        asyncio.create_task(writer("https://two.example")),
    ]
    await asyncio.sleep(0)
    start.set()
    results = await asyncio.gather(*tasks)

    rows = await _rows(pg, tenant_id="acme", idempotency_key=key)
    assert len(rows) == 1, "the unique constraint did not serialise the race"
    assert results[0] == results[1] == rows[0].id


# ── Schema shape ───────────────────────────────────────────────────────────


async def test_uniqueness_is_composite_not_global(pg) -> None:
    """The constraint the ORM declares is the one the server actually holds."""
    async with pg.begin() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT con.conname,
                       (SELECT array_agg(att.attname ORDER BY att.attname)
                          FROM unnest(con.conkey) AS k(attnum)
                          JOIN pg_attribute att
                            ON att.attrelid = con.conrelid AND att.attnum = k.attnum)
                         AS cols
                  FROM pg_constraint con
                  JOIN pg_class rel ON rel.oid = con.conrelid
                 WHERE rel.relname = 'enrichment_results' AND con.contype = 'u'
                """
            )
        )
        constraints = {name: set(cols) for name, cols in rows.all()}

    assert {"tenant_id", "idempotency_key"} in constraints.values()
    assert {"idempotency_key"} not in constraints.values(), (
        "a global UNIQUE on idempotency_key still exists — one tenant's key "
        "would again block every other tenant's"
    )
