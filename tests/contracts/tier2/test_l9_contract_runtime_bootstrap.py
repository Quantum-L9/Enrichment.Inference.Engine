from __future__ import annotations

from fastapi import FastAPI

from app.bootstrap.l9_contract_runtime import (
    get_l9_contract_runtime_state,
    install_l9_contract_controls,
)

pytest_plugins: list[str] = []


def _registered_paths(app: FastAPI) -> set[str]:
    """Collect route paths, including FastAPI `_IncludedRouter` mounts."""
    paths: set[str] = set()
    for route in app.router.routes:
        path = getattr(route, "path", None)
        if path:
            paths.add(path)
        original = getattr(route, "original_router", None)
        if original is None:
            continue
        for nested in getattr(original, "routes", []):
            nested_path = getattr(nested, "path", None)
            if nested_path:
                paths.add(nested_path)
    return paths


def test_install_l9_contract_controls_registers_attestation_route() -> None:
    app = FastAPI()
    installed = install_l9_contract_controls(app)
    assert installed is app
    assert "/v1/attestation" in _registered_paths(app)


def test_get_l9_contract_runtime_state_reads_initialized_state() -> None:
    app = FastAPI()
    install_l9_contract_controls(app)
    app.state.l9_contract_control = {
        "node_id": "enrichment-engine",
        "node_version": "2.3.0",
        "contract_version": "1.0.0",
        "contract_digest": "abc123",
        "policy_mode": "enforced",
        "degraded_modes": [],
    }
    state = get_l9_contract_runtime_state(app)
    assert state["node_id"] == "enrichment-engine"
    assert state["policy_mode"] == "enforced"
