"""
L9 Contract Runtime Bootstrap — chassis layer.

Installs constitution/attestation startup gates and contract control state
onto the FastAPI application object.

L9 Architecture Note:
This module is CHASSIS. It is called exclusively from ``app/main.py``
during application lifespan setup. FastAPI imports are permitted here
(INV-ARCH-03: engine MUST NOT import fastapi; chassis owns the app
object — §2.1).

Engine modules must NEVER import this file. Engine code that needs
contract-runtime state receives it as a plain dict via dependency
injection from the chassis (INV-ARCH-04, INV-ARCH-05).

# L9-fix: ARCH-001
# L9-file: app/bootstrap/l9_contract_runtime.py
# L9-violation: Missing chassis-layer metadata tag — ARCH-001 scanner fired on FastAPI imports
# L9-fix-summary: Added # L9-layer: chassis header and architecture note per INV-ARCH-03
# L9-layer: chassis
# L9-node: enrichment-inference-engine
# L9-contract-version: 1.0.0
"""

from __future__ import annotations

from typing import Any

# FastAPI import is PERMITTED — this is a chassis bootstrap module.
# Only app/main.py (chassis) should import this file.
from fastapi import FastAPI

from app.api.v1.attestation import router as attestation_router
from app.services.runtime_attestation import build_runtime_attestation
from scripts.l9_contract_control import verify_attestation, verify_constitution


def _route_exists(app: FastAPI, path: str) -> bool:
    return any(getattr(route, "path", None) == path for route in app.router.routes)


def install_l9_contract_controls(app: FastAPI) -> FastAPI:
    """
    Install L9 contract attestation routes and register startup validation that enforces constitution and runtime attestation.
    
    Registers the attestation router at "/v1/attestation" if not already present, and adds a startup event that runs constitution and attestation verifications. On successful startup verification, stores the runtime attestation state on app.state.l9_contract_control.
    
    Parameters:
        app (FastAPI): The FastAPI application to modify.
    
    Returns:
        FastAPI: The same FastAPI application with L9 contract controls installed.
    
    Raises:
        RuntimeError: If constitution verification or runtime attestation verification fails during startup.
    """
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
    """
    Retrieve the L9 contract runtime state dictionary stored on the FastAPI application's state.
    
    Returns:
        dict[str, Any]: Runtime state containing L9 contract metadata (e.g. node_id, node_version, contract_version, contract_digest, policy_mode, degraded_modes).
    
    Raises:
        RuntimeError: If the L9 contract runtime controls are not installed or not yet initialized.
    """
    state = getattr(app.state, "l9_contract_control", None)
    if not isinstance(state, dict):
        raise RuntimeError("L9 contract runtime controls not installed or not initialized")
    return state
