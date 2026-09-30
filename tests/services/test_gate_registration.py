"""Gate registration boundary: EIE owns the values, Gate_SDK owns participation.

EIE's bespoke registration client, its re-registration loop and its
registration state are gone (L9-PARTICIPATION-01): Gate_SDK's create_node_app()
registers, re-registers after a Gate restart and tracks the state. What remains
testable here is the half EIE still owns — node identity, advertised actions,
health endpoint, owner and node cap — that EIE hands exactly that identity to
the SDK, and that /api/v1/health and /api/v1/ready keep their contract on top
of the SDK's state.

Hermetic: no network is contacted.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.update(
    {
        "PERPLEXITY_API_KEY": "test-key",
        "API_SECRET_KEY": "test-secret-key-32-chars-long!!",
        "API_KEY_HASH": "d74ff0ee8da3b9806b18c877dbf29bbde50b5bd8e4dad7a3a725000feb82e8f1",
        "KB_DIR": "./kb",
        "REDIS_URL": "redis://localhost:6379/0",
    }
)

import pytest
from constellation_node_sdk import NodeParticipation, ParticipationState
from fastapi.testclient import TestClient

from app import main as main_module
from app.core.config import Settings
from app.main import (
    ADVERTISED_ACTIONS,
    NODE_TIMEOUT_MS,
    build_node_registration,
)
from app.services.request_deadline import CANONICAL_CONVERGE_BUDGET_SECONDS

GATE_URL = "http://gate.test"


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


# --------------------------------------------------------------------------
# Participation is the SDK's: EIE hands it the identity and nothing else
# --------------------------------------------------------------------------


def test_eie_participates_through_the_sdk_with_its_own_identity():
    """No bespoke registration code: the app's participation is the SDK's."""
    participation = main_module.app.state.participation
    assert isinstance(participation, NodeParticipation)
    for gone in (
        "_register_with_gate",
        "_reregistration_loop",
        "start_reregistration_loop",
        "stop_reregistration_loop",
        "_gate_registered",
    ):
        assert not hasattr(main_module, gone), f"app.main still defines {gone}"


def _with_state(monkeypatch, state: str) -> None:
    fake = SimpleNamespace(status=SimpleNamespace(state=ParticipationState(state)))
    monkeypatch.setattr(main_module.app.state, "participation", fake)


@pytest.mark.parametrize(
    ("sdk_state", "expected_state", "expected_status", "expected_registered"),
    [
        ("not_attempted", "not_attempted", "degraded", None),
        ("registering", "not_attempted", "degraded", None),
        ("active", "registered", "ok", True),
        ("degraded", "failed", "degraded", False),
        ("disabled", "disabled", "ok", None),
    ],
)
def test_health_surfaces_the_sdk_participation_state(
    monkeypatch, sdk_state, expected_state, expected_status, expected_registered
):
    """Liveness is not routability: an unregistered node reports degraded (EIE-002)."""
    _with_state(monkeypatch, sdk_state)
    body = TestClient(main_module.app).get("/api/v1/health").json()
    assert body["gate_registration"] == expected_state
    assert body["status"] == expected_status
    assert body["gate_registered"] is expected_registered


# --------------------------------------------------------------------------
# EIE-212-F003: readiness is a status code a probe can read, liveness stays 200
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sdk_state", "expected_state", "expected_http", "expected_ready"),
    [
        ("not_attempted", "not_attempted", 503, False),
        ("degraded", "failed", 503, False),
        ("active", "registered", 200, True),
        ("disabled", "disabled", 200, True),
    ],
)
def test_readiness_fails_at_http_level_while_liveness_stays_up(
    monkeypatch, sdk_state, expected_state, expected_http, expected_ready
):
    """A readinessProbe reads the status code; a Gate outage must not become a restart loop."""
    _with_state(monkeypatch, sdk_state)
    client = TestClient(main_module.app)
    ready = client.get(main_module.READINESS_ENDPOINT)
    live = client.get(main_module.HEALTH_ENDPOINT)

    assert ready.status_code == expected_http
    body = ready.json()
    assert body["ready"] is expected_ready
    assert body["status"] == ("ready" if expected_ready else "not_ready")
    assert body["gate_registration"] == expected_state
    assert live.status_code == 200


def test_readiness_endpoint_is_distinct_from_the_registered_health_endpoint():
    """Gate polls health_endpoint for liveness; the probe path must not alias it."""
    assert main_module.READINESS_ENDPOINT == "/api/v1/ready"
    assert main_module.READINESS_ENDPOINT != main_module.HEALTH_ENDPOINT
    assert build_node_registration(_settings()).health_endpoint == main_module.HEALTH_ENDPOINT


def test_registration_default_matches_the_documented_contract():
    """EIE-002: the code default and .env.example must not disagree."""
    env_example = Path(__file__).resolve().parents[2] / ".env.example"
    assert "GATE_REGISTRATION_ENABLED=true" in env_example.read_text(encoding="utf-8")
    assert Settings.model_fields["gate_registration_enabled"].default is True


def test_settings_bridge_exports_file_only_gate_controls(monkeypatch):
    """`.env` values reach the SDK only if this process exports them."""
    monkeypatch.delenv("GATE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("GATE_REGISTRATION_ENABLED", raising=False)
    main_module._bridge_settings_to_participation_env(
        _settings(gate_admin_token="from-dotenv", gate_registration_enabled=False)
    )
    assert os.environ["GATE_ADMIN_TOKEN"] == "from-dotenv"
    assert os.environ["GATE_REGISTRATION_ENABLED"] == "false"


def test_settings_bridge_does_not_override_process_env(monkeypatch):
    """Kubernetes and the shell stay authoritative over the env file."""
    monkeypatch.setenv("GATE_ADMIN_TOKEN", "from-process")
    monkeypatch.setenv("GATE_REGISTRATION_ENABLED", "true")
    main_module._bridge_settings_to_participation_env(
        _settings(gate_admin_token="from-dotenv", gate_registration_enabled=False)
    )
    assert os.environ["GATE_ADMIN_TOKEN"] == "from-process"
    assert os.environ["GATE_REGISTRATION_ENABLED"] == "true"
