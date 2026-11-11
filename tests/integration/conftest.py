"""
Integration test fixtures — Neo4j (service or testcontainer) + FastAPI test client.
Requires: pytest-asyncio, httpx; testcontainers[neo4j] only when NEO4J_TEST_URI is unset.
"""

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import skip_or_fail_unavailable


@pytest.fixture(scope="session")
def neo4j_container():
    """Neo4j connection details: an existing instance, else a testcontainer.

    NEO4J_TEST_URI / NEO4J_TEST_USER / NEO4J_TEST_PASSWORD name an instance that is
    already running (CI's `integration` job service container). Otherwise a Neo4j 5
    Community testcontainer (no licence needed) is started. With neither available
    the fixture skips — or fails under EIE_REQUIRE_INTEGRATION=1.
    """
    uri = os.getenv("NEO4J_TEST_URI")
    if uri:
        yield {
            "uri": uri,
            "user": os.getenv("NEO4J_TEST_USER", "neo4j"),
            "password": os.getenv("NEO4J_TEST_PASSWORD", "l9-test-password"),
        }
        return
    try:
        from testcontainers.neo4j import Neo4jContainer
    except ImportError:
        skip_or_fail_unavailable("testcontainers not installed and NEO4J_TEST_URI not set")
        return
    with Neo4jContainer(image="neo4j:5") as container:
        yield {
            "uri": container.get_connection_url(),
            "user": "neo4j",
            "password": container.password,
        }


@pytest_asyncio.fixture
async def api_client():
    """AsyncClient pointed at FastAPI app."""
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="https://testserver",
        headers={"Authorization": "Bearer test-key"},
    ) as client:
        yield client


@pytest.fixture
def sample_enrich_payload():
    return {
        "entity_id": "test-entity-001",
        "entity_type": "facility",
        "domain": "plasticos",
        "fields": {
            "company_name": "Alpha Recyclers LLC",
            "materials_handled": ["HDPE", "LDPE"],
            "city": "Charlotte",
            "state": "NC",
        },
    }
