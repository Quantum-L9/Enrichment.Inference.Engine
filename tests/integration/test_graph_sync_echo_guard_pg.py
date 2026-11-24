"""G4 echo guard on PostgreSQL: which field values the intent history proves settled.

Contract: docs/contracts/data/graph_sync_intents.md *Field set and provenance* (G4).
Needs a PostgreSQL the test may own: ``EIE_TEST_DATABASE_URL``. Every test drops
and recreates the schema. Without it the module skips -- or fails under
``EIE_REQUIRE_INTEGRATION=1``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from app.services import pg_store
from tests.conftest import skip_or_fail_unavailable

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

DB_URL = os.environ.get("EIE_TEST_DATABASE_URL", "")
TENANT, ENTITY, ENDPOINT = "synthco", "st-1", "stations"


@pytest_asyncio.fixture
async def db() -> AsyncIterator[None]:
    if not DB_URL:
        skip_or_fail_unavailable("EIE_TEST_DATABASE_URL not set")
    pg_store.init_engine(DB_URL, pool_size=5, max_overflow=5)
    engine = pg_store._engine
    assert engine is not None
    try:
        async with engine.begin() as conn:
            await conn.run_sync(pg_store.Base.metadata.drop_all)
            await conn.run_sync(pg_store.Base.metadata.create_all)
    except OSError as exc:
        await pg_store.close_engine()
        skip_or_fail_unavailable(f"PostgreSQL unreachable: {exc}")
    yield
    await pg_store.close_engine()


async def _intent(key: str, state: str, attempts: int, fields: dict[str, Any], **kw: Any) -> None:
    async with pg_store.get_session() as session:
        session.add(
            pg_store.GraphSyncIntent(
                tenant_id=kw.get("tenant", TENANT),
                effect_key=key,
                entity_id=kw.get("entity", ENTITY),
                endpoint=kw.get("endpoint", ENDPOINT),
                origin="converge",
                fields=fields,
                state=state,
                attempts=attempts,
            )
        )


async def test_an_acknowledged_history_settles_its_labelled_value(db: None) -> None:
    await _intent("k1", "acknowledged", 1, {"band": "B2", "band_provenance": "enriched"})
    settled = await pg_store.graph_sync_settled_fields(TENANT, ENTITY, ENDPOINT)
    assert settled == {"band": {"band": "B2", "band_provenance": "enriched"}}


async def test_a_sent_unacknowledged_intent_unsettles_its_field(db: None) -> None:
    await _intent("k1", "acknowledged", 1, {"band": "B2", "band_provenance": "enriched"})
    await _intent("k2", "failed_terminal", 3, {"band": "B3", "band_provenance": "observed"})
    assert await pg_store.graph_sync_settled_fields(TENANT, ENTITY, ENDPOINT) == {}


async def test_history_is_scoped_to_the_subject(db: None) -> None:
    await _intent("k1", "acknowledged", 1, {"band": "B2"})
    await _intent("k2", "failed_terminal", 2, {"band": "B9"}, entity="st-2")
    await _intent("k3", "failed_terminal", 2, {"band": "B9"}, endpoint="depots")
    await _intent("k4", "failed_terminal", 2, {"band": "B9"}, tenant="other")
    settled = await pg_store.graph_sync_settled_fields(TENANT, ENTITY, ENDPOINT)
    assert settled == {"band": {"band": "B2"}}
