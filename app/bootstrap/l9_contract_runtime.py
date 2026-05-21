"""
L9 runtime contract bootstrap orchestration.

L9 Architecture Note:
    ``app/bootstrap/l9_contract_runtime.py`` is a CHASSIS bootstrap/runtime
    lifecycle module. FastAPI imports are permitted because this module owns
    startup orchestration, route registration, and runtime attestation wiring
    before engine execution begins.

# L9-node: enrichment-inference-engine
# L9-layer: chassis
# L9-contract-version: 1.0.0
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from app.api.v1.attestation import router as attestation_router
from app.services.runtime_attestation import build_runtime_attestation
from scripts.l9_contract_control import verify_attestation, verify_constitution


def _route_exists(app: FastAPI, path: str) -> bool:
    return any(getattr(route, "path", None) == path for route in app.router.routes)


def install_l9_contract_controls(app: FastAPI) -> FastAPI:
    if not _route_exists(app, "/v1/attestation"):
        app.include_router(attestation_router)

    @app.on_event("startup")
    def _l9_contract_startup_validation() -> None:
        constitution_ok, constitution_errors = verify_constitution()
        if not constitution_ok:
            raise RuntimeError(
                "constitution verification failed at startup: " + "; ".join(constitution_errors)
            )

        attestation_ok, attestation_errors = verify_attestation()
        if not attestation_ok:
            raise RuntimeError(
                "runtime attestation verification failed at startup: "
                + "; ".join(attestation_errors)
            )

        attestation = build_runtime_attestation()
        app.state.l9_contract_control = {
            "node_id": attestation["node_id"],
            "node_version": attestation["node_version"],
            "contract_version": attestation["contract_version"],
            "contract_digest": attestation["contract_digest"],
            "policy_mode": attestation["policy_mode"],
            "degraded_modes": attestation["degraded_modes"],
        }

    return app


def get_l9_contract_runtime_state(app: FastAPI) -> dict[str, Any]:
    state = getattr(app.state, "l9_contract_control", None)
    if not isinstance(state, dict):
        raise RuntimeError("L9 contract runtime controls not installed or not initialized")
    return state
