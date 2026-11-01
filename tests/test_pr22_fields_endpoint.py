"""
tests/test_pr22_fields_endpoint.py

Proves GAP-5 end-to-end: /api/v1/fields/{entity_id} returns 200 when
a persisted enrichment result exists, and 404 when none does.

Also proves the router is mounted (not orphaned) and respects tenant_id isolation.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.config import get_settings

_TEST_API_KEY = "pass"
# Precomputed sha256(_TEST_API_KEY) — the same literal digest already used by
# tests/test_api.py, tests/test_rate_limiter.py and tests/services/
# test_gate_registration.py. Stored rather than computed so no hashing call
# exists here at all: hashing a fixture key inline is what CodeQL flags as
# py/weak-sensitive-data-hashing (SHA-256 is not a computationally expensive
# password hash), and usedforsecurity=False does not suppress that query.
# app/core/auth.py still does the real comparison via hmac.compare_digest.
_TEST_API_KEY_HASH = "d74ff0ee8da3b9806b18c877dbf29bbde50b5bd8e4dad7a3a725000feb82e8f1"
AUTH = {"X-API-Key": _TEST_API_KEY}


@pytest.fixture(scope="module", autouse=True)
def _module_env() -> Iterator[None]:
    """Scope API_KEY_HASH to this module's tests.

    This was a module-level ``os.environ[...] =`` plus ``cache_clear()``, which
    pytest runs during collection and therefore for the whole session.
    ``patch.dict`` restores the caller's environment on teardown, and the
    settings cache is cleared on both sides so neither this module nor the next
    reads a ``Settings`` built from the other's environment.
    """
    with patch.dict(os.environ, {"API_KEY_HASH": _TEST_API_KEY_HASH}):
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


def _make_mock_result(entity_id: str = "ent-001", tenant_id: str = "test-tenant"):
    """Plain result object — ORM EnrichmentResult.__new__ has no session identity."""
    return SimpleNamespace(
        id="uuid-001",
        tenant_id=tenant_id,
        entity_id=entity_id,
        object_type="Account",
        fields={"material_type": "HDPE", "facility_tier": "tier-2"},
        confidence=0.85,
        state="completed",
        pass_count=2,
        tokens_used=480,
        processing_time_ms=1100,
        created_at=datetime.now(UTC),
    )


class _FakeResultStore:
    """In-memory ResultStore stand-in — no Redis / pg session."""

    latest = None
    history: list = []
    captured_tenants: list[str] = []

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        _FakeResultStore.captured_tenants.append(tenant_id)

    async def get_latest_for_entity(self, entity_id: str):
        return _FakeResultStore.latest

    history_calls: list[dict] = []

    async def get_field_confidence_history(
        self, entity_id: str, field_name: str = "", enrichment_result_id=None
    ):
        _FakeResultStore.history_calls.append(
            {"field_name": field_name, "enrichment_result_id": enrichment_result_id}
        )
        return list(_FakeResultStore.history)


def _reset_fake_store(latest=None, history: list | None = None) -> None:
    _FakeResultStore.latest = latest
    _FakeResultStore.history = history or []
    _FakeResultStore.captured_tenants = []
    _FakeResultStore.history_calls = []


@pytest.mark.asyncio
async def test_fields_endpoint_404_when_no_result():
    """Without a persisted result, /fields must return 404."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    _reset_fake_store(latest=None)
    with patch("app.api.v1.fields.ResultStore", _FakeResultStore):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            resp = await client.get(
                "/api/v1/fields/ent-001",
                params={"tenant_id": "test-tenant"},
                headers=AUTH,
            )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_fields_endpoint_200_after_persist():
    """After GAP-5 fix: a persisted result must yield 200 with correct field map."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    _reset_fake_store(latest=_make_mock_result(), history=[])
    with patch("app.api.v1.fields.ResultStore", _FakeResultStore):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            resp = await client.get(
                "/api/v1/fields/ent-001",
                params={"tenant_id": "test-tenant"},
                headers=AUTH,
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["entity_id"] == "ent-001"
    assert "material_type" in body["fields"]
    assert "facility_tier" in body["fields"]
    assert body["fields"]["material_type"]["value"] == "HDPE"
    assert 0.0 <= body["avg_confidence"] <= 1.0


@pytest.mark.asyncio
async def test_fields_endpoint_confidence_history_used_when_present():
    """Confidence history is used in preference to response.confidence when available."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    history = [
        {
            "confidence": 0.91,
            "source": "consensus",
            "pass_number": 2,
        }
    ]
    _reset_fake_store(latest=_make_mock_result(), history=history)
    with patch("app.api.v1.fields.ResultStore", _FakeResultStore):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            resp = await client.get(
                "/api/v1/fields/ent-001/material_type/history",
                params={"tenant_id": "test-tenant"},
                headers=AUTH,
            )

    assert resp.status_code == 200
    entries = resp.json()
    assert isinstance(entries, list)
    assert entries[0]["confidence"] == pytest.approx(0.91)
    assert entries[0]["source"] == "consensus"


@pytest.mark.asyncio
async def test_fields_endpoint_tenant_isolation():
    """GET /fields must use the tenant_id query param for ResultStore scoping."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    _reset_fake_store(latest=None)
    with patch("app.api.v1.fields.ResultStore", _FakeResultStore):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            await client.get(
                "/api/v1/fields/ent-001",
                params={"tenant_id": "specific-tenant"},
                headers=AUTH,
            )

    assert "specific-tenant" in _FakeResultStore.captured_tenants


async def _get_fields(latest, history=None):
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    _reset_fake_store(latest=latest, history=history)
    with patch("app.api.v1.fields.ResultStore", _FakeResultStore):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            return await client.get(
                "/api/v1/fields/ent-001",
                params={"tenant_id": "test-tenant"},
                headers=AUTH,
            )


@pytest.mark.asyncio
async def test_fields_endpoint_reads_the_newest_runs_provenance():
    """WU-19 / F-042: field history is read for the latest result only."""
    latest = _make_mock_result()
    history = [{"confidence": 0.93, "source": "consensus", "pass_number": 1}]
    resp = await _get_fields(latest, history)

    assert resp.status_code == 200
    assert _FakeResultStore.history_calls
    assert all(c["enrichment_result_id"] == latest.id for c in _FakeResultStore.history_calls)
    assert resp.json()["fields"]["material_type"]["confidence"] == 0.93


@pytest.mark.asyncio
async def test_fields_endpoint_coverage_uses_the_requested_schema():
    """WU-19 / F-042: 2 of 4 requested fields filled -> coverage 0.5, not 1.0."""
    latest = _make_mock_result()
    latest.feature_vector = {
        "requested_fields": ["material_type", "facility_tier", "employees", "website"]
    }
    resp = await _get_fields(latest)

    assert resp.status_code == 200
    assert resp.json()["coverage_ratio"] == 0.5


@pytest.mark.asyncio
async def test_fields_endpoint_coverage_counts_attempted_but_unfilled_fields():
    """Without a recorded schema, tracked-but-unfilled fields still count against coverage."""
    latest = _make_mock_result()
    latest.feature_vector = {
        "confidence_tracking": {
            "fields": {
                "material_type": {"confidence": 0.9},
                "facility_tier": {"confidence": 0.9},
                "employees": {"confidence": 0.1},
                "website": {"confidence": 0.1},
            }
        }
    }
    resp = await _get_fields(latest)

    assert resp.json()["coverage_ratio"] == 0.5
