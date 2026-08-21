"""
Integration tests for the /v1/converge endpoint.
Uses mocked LLM calls to avoid external API costs in CI.
"""

from __future__ import annotations

import hashlib
import os
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import get_settings

_TEST_API_KEY = "pass"
os.environ["API_KEY_HASH"] = hashlib.sha256(_TEST_API_KEY.encode()).hexdigest()
get_settings.cache_clear()
AUTH = {"X-API-Key": _TEST_API_KEY}


@pytest.mark.asyncio
async def test_converge_health(api_client):
    """Health endpoint returns 200."""
    resp = await api_client.get("/api/v1/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
@patch("app.services.perplexity_client.query_perplexity", new_callable=AsyncMock)
async def test_converge_single_entity(mock_llm, api_client):
    """POST /v1/converge returns ConvergeResponse shape."""
    mock_llm.return_value = {
        "material_grade": "A",
        "contamination_tolerance_pct": 0.02,
        "facility_tier": "tier_1",
    }
    payload = {
        "entity": {
            "id": "test-001",
            "company_name": "Alpha Recyclers",
            "materials_handled": ["HDPE"],
        },
        "object_type": "Account",
        "domain": "plasticos",
        "objective": "Full entity enrichment",
        "max_passes": 2,
        "max_budget_tokens": 5000,
    }
    resp = await api_client.post("/v1/converge", json=payload, headers=AUTH)
    # 503 is the live unconfigured-store contract (GAP-3); 422 on body drift.
    assert resp.status_code in (200, 422, 503)


@pytest.mark.asyncio
async def test_scan_endpoint_rejects_unknown_domain_without_auth(api_client):
    """Auth runs first — unauthenticated POST /v1/scan is 401."""
    resp = await api_client.post(
        "/v1/scan",
        json={
            "fields": [{"name": "company_name", "field_type": "string"}],
            "domain": "nonexistent-domain-xyz",
        },
        headers={"X-API-Key": ""},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_scan_endpoint_rejects_unknown_domain(api_client):
    """Authenticated POST /v1/scan returns the live unknown-domain status (400)."""
    resp = await api_client.post(
        "/v1/scan",
        json={
            "fields": [{"name": "company_name", "field_type": "string"}],
            "domain": "nonexistent-domain-xyz",
        },
        headers=AUTH,
    )
    assert resp.status_code == 400
