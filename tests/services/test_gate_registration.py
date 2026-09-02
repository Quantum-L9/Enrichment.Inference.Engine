"""Gate registration boundary: EIE owns the values, Gate_SDK owns the transport.

EIE's bespoke registration client is gone. What remains testable here is the
half EIE still owns — node identity, advertised actions, health endpoint, owner,
and the node cap it advertises — plus proof that the wire body the SDK renders
from it is semantically what the Gate previously accepted.

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
from app.main import (
    ADVERTISED_ACTIONS,
    NODE_TIMEOUT_MS,
    _register_with_gate,
    build_node_registration,
)
from app.services.request_deadline import CANONICAL_CONVERGE_BUDGET_SECONDS

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


# --------------------------------------------------------------------------
# EIE's half: the values
# --------------------------------------------------------------------------


def test_registration_carries_eie_semantics():
    reg = build_node_registration(_settings())
    assert reg.node_name == "enrichment-engine"
    assert reg.owner == "eie"
    assert reg.node_type == "enrichment"
    assert reg.version == "2.3.0"
    assert reg.health_endpoint == "/api/v1/health"
    assert "converge" in reg.supported_actions
    assert reg.internal_url == "http://enrichment-engine:8000"


def test_advertised_node_cap_equals_eie_operation_ceiling():
    """Gate bounds a worker with min(remaining budget, node cap).

    Advertising a cap larger than EIE's own complete-operation ceiling would
    tell Gate to wait on time EIE has already given up on — a second clock.
    """
    reg = build_node_registration(_settings())
    assert reg.timeout_ms == NODE_TIMEOUT_MS
    assert reg.timeout_ms == int(CANONICAL_CONVERGE_BUDGET_SECONDS * 1000) == 25_000


def test_advertised_actions_are_a_subset_of_runtime_allowed():
    """advertised ⊆ runtime_allowed — never auto-advertise everything permitted."""
    from app.main import _build_runtime_config

    allowed = set(_build_runtime_config().allowed_actions)
    advertised = set(ADVERTISED_ACTIONS)
    assert advertised <= allowed, f"advertised beyond runtime: {advertised - allowed}"
    assert advertised < allowed, "advertising every runtime-allowed action is the drift"


def test_advertised_actions_are_all_implemented():
    """advertised ⊆ implemented — never advertise an action EIE cannot serve."""
    from constellation_node_sdk.runtime.handlers import clear_handlers, registered_actions

    from app.engines.orchestration_layer import register as register_orchestration
    from app.services.chassis_handlers import register_all_handlers

    clear_handlers()
    try:
        register_orchestration(kb=None, idem_store=None)
        register_all_handlers()
        implemented = set(registered_actions())
    finally:
        clear_handlers()

    missing = set(ADVERTISED_ACTIONS) - implemented
    assert not missing, f"advertised but not implemented: {sorted(missing)}"


# --------------------------------------------------------------------------
# The SDK's half: the wire body, and semantic equivalence with the old client
# --------------------------------------------------------------------------

# The exact body the deleted bespoke client sent, and the Gate accepted.
_LEGACY_NODE_BODY = {
    "internal_url": "http://enrichment-engine:8000",
    "supported_actions": ["converge", "graph-inference-result", "enrich", "enrich-and-sync"],
    "health_endpoint": "/api/v1/health",
    "metadata": {"owner": "eie", "version": "2.3.0", "type": "enrichment"},
}


def test_sdk_payload_is_semantically_equivalent_to_the_deleted_client():
    """Every field the Gate resolved identity/routing from is unchanged.

    Byte equality is not required: the SDK legitimately adds control-plane
    metadata (`generated_by`) and explicit routing defaults the old body left
    to the Gate. What must not move is anything Gate reads to decide *who owns
    which action and where to reach them*.
    """
    node = build_node_registration(_settings()).to_payload()["enrichment-engine"]

    for key in ("internal_url", "supported_actions", "health_endpoint"):
        assert node[key] == _LEGACY_NODE_BODY[key], f"{key} drifted"
    for key in ("owner", "version", "type"):
        assert node["metadata"][key] == _LEGACY_NODE_BODY["metadata"][key], (
            f"metadata.{key} drifted"
        )

    # SDK-added control-plane metadata is permitted, and identified.
    assert node["metadata"]["generated_by"] == "constellation-node-sdk"

    # Fields the Gate rejects outright must still be absent.
    assert "execute_path" not in node
    assert "health_path" not in node
    assert "owner" not in node  # owner is metadata.owner, never top-level


@respx.mock
async def test_register_posts_canonical_payload():
    route = respx.post(REGISTER_URL).mock(return_value=httpx.Response(200))
    assert await _register_with_gate(_settings()) is True

    request = route.calls.last.request
    assert request.url.params.get("overwrite") == "true"
    node = json.loads(request.content)["enrichment-engine"]
    assert node["metadata"]["owner"] == "eie"
    assert node["health_endpoint"] == "/api/v1/health"
    assert "converge" in node["supported_actions"]
    assert node["timeout_ms"] == 25_000


@respx.mock
async def test_admin_token_header_sent_when_configured():
    route = respx.post(REGISTER_URL).mock(return_value=httpx.Response(200))
    await _register_with_gate(_settings(gate_admin_token="secret-token"))
    assert route.calls.last.request.headers.get("X-Admin-Token") == "secret-token"


async def test_disabled_returns_none_and_no_http():
    # No respx router active; a real POST would raise, proving no call is made.
    assert await _register_with_gate(_settings(gate_registration_enabled=False)) is None


async def test_no_gate_url_returns_none_and_no_http():
    assert await _register_with_gate(_settings(gate_url="")) is None


@respx.mock
async def test_rejection_returns_false():
    respx.post(REGISTER_URL).mock(return_value=httpx.Response(422))
    assert await _register_with_gate(_settings()) is False


@respx.mock
async def test_transport_error_is_non_fatal():
    """Gate unreachable degrades readiness; it never raises into startup."""
    respx.post(REGISTER_URL).mock(side_effect=httpx.ConnectError("boom"))
    assert await _register_with_gate(_settings()) is False


@pytest.mark.parametrize(
    ("registered", "expected_status"),
    [(None, "ok"), (True, "ok"), (False, "degraded")],
)
def test_health_surfaces_gate_registered(monkeypatch, registered, expected_status):
    """Liveness is not routability: an unregistered node reports degraded."""
    import app.main as main

    monkeypatch.setattr(main, "_gate_registered", registered)
    client = TestClient(main.app)
    body = client.get("/api/v1/health").json()
    assert body["gate_registered"] is registered
    assert body["status"] == expected_status


# --------------------------------------------------------------------------
# Periodic re-registration: routing recovery without a process restart
# --------------------------------------------------------------------------

import asyncio  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from app import main as main_module  # noqa: E402
from app.main import start_reregistration_loop, stop_reregistration_loop  # noqa: E402


def test_reregistration_loop_is_off_when_registration_is_off():
    assert start_reregistration_loop(_settings(gate_registration_enabled=False)) is None
    assert start_reregistration_loop(_settings(gate_url="")) is None


@pytest.mark.asyncio
async def test_reregistration_loop_is_off_at_zero_interval():
    assert start_reregistration_loop(_settings(gate_reregistration_interval_seconds=0)) is None


@pytest.mark.asyncio
async def test_reregistration_loop_reregisters_and_updates_readiness(monkeypatch):
    """Each cycle re-runs the SDK registration and the verdict feeds readiness."""
    verdicts = iter([False, True, True, True, True])
    fake = AsyncMock(side_effect=lambda settings: next(verdicts))
    monkeypatch.setattr(main_module, "_register_with_gate", fake)
    monkeypatch.setattr(main_module, "_gate_registered", None)

    task = start_reregistration_loop(_settings(gate_reregistration_interval_seconds=0.01))
    assert task is not None
    monkeypatch.setattr(main_module, "_reregistration_task", task)
    try:
        for _ in range(200):
            if fake.await_count >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        await stop_reregistration_loop()

    assert fake.await_count >= 2
    assert task.done()
    assert main_module._gate_registered is True


@pytest.mark.asyncio
async def test_reregistration_loop_survives_a_raising_attempt(monkeypatch):
    calls = {"n": 0}

    async def flaky(settings):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("gate exploded")
        return True

    monkeypatch.setattr(main_module, "_register_with_gate", flaky)
    monkeypatch.setattr(main_module, "_gate_registered", None)
    task = start_reregistration_loop(_settings(gate_reregistration_interval_seconds=0.01))
    assert task is not None
    monkeypatch.setattr(main_module, "_reregistration_task", task)
    try:
        for _ in range(200):
            if calls["n"] >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        await stop_reregistration_loop()

    assert calls["n"] >= 2
    assert main_module._gate_registered is True


# --------------------------------------------------------------------------
# Node-runtime signing posture comes from the environment (SDK L9_* names)
# --------------------------------------------------------------------------


def test_runtime_signs_responses_when_key_material_is_present(monkeypatch):
    from app.main import _build_runtime_config

    monkeypatch.setenv("L9_SIGNING_KEY", "worker-material")
    monkeypatch.setenv("L9_SIGNING_KEY_ID", "eie-k1")
    monkeypatch.setenv("L9_VERIFYING_KEYS_JSON", '{"gate-k1": "gate-material"}')
    monkeypatch.setenv("L9_REQUIRE_SIGNATURE", "true")
    monkeypatch.delenv("L9_SIGNING_ALGORITHM", raising=False)
    config = _build_runtime_config()
    assert config.signing_key == "worker-material"
    assert config.signing_key_id == "eie-k1"
    assert config.signing_algorithm == "hmac-sha256"
    assert config.require_signature is True
    assert config.verifying_keys == {"gate-k1": "gate-material"}


def test_runtime_is_unsigned_without_key_material(monkeypatch):
    from app.main import _build_runtime_config

    for name in (
        "L9_SIGNING_KEY",
        "L9_SIGNING_SECRET",
        "L9_SIGNING_KEY_ID",
        "L9_VERIFYING_KEYS_JSON",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("L9_REQUIRE_SIGNATURE", "false")
    config = _build_runtime_config()
    assert config.signing_key is None
    assert config.signing_key_id is None
    assert config.require_signature is False
    assert config.verifying_keys == {}


def test_malformed_verifying_keys_fail_closed(monkeypatch):
    from app.main import _build_runtime_config

    monkeypatch.setenv("L9_VERIFYING_KEYS_JSON", '["not", "a", "map"]')
    with pytest.raises(ValueError, match="L9_VERIFYING_KEYS_JSON"):
        _build_runtime_config()
