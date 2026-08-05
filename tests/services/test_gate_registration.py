"""Tests for explicit Gate registration (TASK-003).

Hermetic: all HTTP is mocked via respx. No real network is contacted.
"""

from __future__ import annotations

import json
import os

os.environ.update(
    {
        "PERPLEXITY_API_KEY": "test-key",
        "API_SECRET_KEY": "test-secret-key-32-chars-long!!",
        "API_KEY_HASH": "d74ff0ee8da3b9806b18c877dbf29bbde50b5bd8e4dad7a3a725000feb82e8f1",
        "KB_DIR": "./kb",
        "REDIS_URL": "redis://localhost:6379/0",
    }
)

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.gate_registration import build_payload, register_with_gate

GATE_URL = "http://gate.test"
REGISTER_URL = f"{GATE_URL}/v1/admin/register"


def _settings(**overrides) -> Settings:
    base = {
        "gate_registration_enabled": True,
        "gate_url": GATE_URL,
        "gate_internal_url": "http://enrichment-engine:8000",
    }
    base.update(overrides)
    return Settings(**base)


def test_build_payload_shape():
    payload = build_payload(_settings())
    assert set(payload.keys()) == {"enrichment-engine"}
    node = payload["enrichment-engine"]
    assert "converge" in node["supported_actions"]
    assert node["health_endpoint"] == "/api/v1/health"
    assert node["metadata"]["owner"] == "eie"
    assert node["internal_url"].startswith("http")
    # Forbidden fields the Gate never accepts.
    assert "execute_path" not in node
    assert "health_path" not in node
    assert "owner" not in node


@respx.mock
async def test_register_posts_canonical_payload():
    route = respx.post(REGISTER_URL).mock(return_value=httpx.Response(200))
    result = await register_with_gate(_settings())
    assert result is True

    request = route.calls.last.request
    assert request.url.params.get("overwrite") == "true"
    body = json.loads(request.content)
    assert "enrichment-engine" in body
    node = body["enrichment-engine"]
    assert "converge" in node["supported_actions"]
    assert node["health_endpoint"] == "/api/v1/health"
    assert node["metadata"]["owner"] == "eie"
    assert node["internal_url"].startswith("http")


@respx.mock
async def test_admin_token_header_sent_when_configured():
    route = respx.post(REGISTER_URL).mock(return_value=httpx.Response(200))
    await register_with_gate(_settings(gate_admin_token="secret-token"))
    assert route.calls.last.request.headers.get("X-Admin-Token") == "secret-token"


async def test_disabled_returns_none_and_no_http():
    # No respx router active; a real POST would raise, proving no call is made.
    result = await register_with_gate(_settings(gate_registration_enabled=False))
    assert result is None


@respx.mock
async def test_rejection_returns_false():
    respx.post(REGISTER_URL).mock(return_value=httpx.Response(422))
    result = await register_with_gate(_settings())
    assert result is False


@respx.mock
async def test_transport_error_is_non_fatal():
    respx.post(REGISTER_URL).mock(side_effect=httpx.ConnectError("boom"))
    result = await register_with_gate(_settings())
    assert result is False


@pytest.mark.parametrize(
    ("registered", "expected_status"),
    [(None, "ok"), (True, "ok"), (False, "degraded")],
)
def test_health_surfaces_gate_registered(monkeypatch, registered, expected_status):
    import app.main as main

    monkeypatch.setattr(main, "_gate_registered", registered)
    client = TestClient(main.app)
    body = client.get("/api/v1/health").json()
    assert body["gate_registered"] is registered
    assert body["status"] == expected_status
