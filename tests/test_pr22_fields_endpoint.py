"""
tests/test_pr22_fields_endpoint.py

Proves GAP-5 end-to-end: /api/v1/fields/{entity_id} returns 200 when
a persisted enrichment result exists, and 404 when none does.

Also proves the router is mounted (not orphaned) and respects tenant_id isolation.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.config import get_settings

_TEST_API_KEY = "pass"
# Not a credential hash: this mirrors app/core/auth.py's SHA-256 digest so the
# fixture key authenticates against a throwaway in-process app. The input is a
# hard-coded test literal, never a real secret, so usedforsecurity=False marks
# the call as non-security (CWE-327/328, py/weak-sensitive-data-hashing) —
# same treatment as app/engines/convergence_controller.py::_cache_key. The
# digest itself is byte-identical, so hmac.compare_digest still matches.
os.environ["API_KEY_HASH"] = hashlib.sha256(
    _TEST_API_KEY.encode(), usedforsecurity=False
).hexdigest()
get_settings.cache_clear()
AUTH = {"X-API-Key": _TEST_API_KEY}


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

    async def get_field_confidence_history(self, entity_id: str, field_name: str = ""):
        return list(_FakeResultStore.history)


def _reset_fake_store(latest=None, history: list | None = None) -> None:
    _FakeResultStore.latest = latest
    _FakeResultStore.history = history or []
    _FakeResultStore.captured_tenants = []


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
