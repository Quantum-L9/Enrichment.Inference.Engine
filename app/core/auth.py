"""
API key authentication — constant-time SHA-256 hash comparison.

Salesforce Named Credential and Odoo ir.config_parameter both store and
send the raw key; we only store the hash.

L9 Architecture Note:
``app/core/auth.py`` is a CHASSIS module — it is responsible for
validating inbound credentials before the request enters the engine.
FastAPI imports are permitted here (INV-ARCH-03: engine MUST NOT
import fastapi; chassis MUST own auth — §2.1).

If any engine module needs to represent "an authenticated identity"
it should receive a resolved ``TenantContext`` object injected by the
chassis, never call this module directly (INV-ARCH-05).

# L9-fix: ARCH-001
# L9-file: app/core/auth.py
# L9-violation: Missing chassis-layer metadata tag — ARCH-001 scanner fired on FastAPI imports
# L9-fix-summary: Added # L9-layer: chassis header and architecture note per INV-ARCH-03
# L9-layer: chassis
# L9-node: enrichment-inference-engine
# L9-contract-version: 1.0.0
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Annotated

# FastAPI Security imports are PERMITTED in chassis auth modules.
# Engine code must never replicate these imports (INV-ARCH-03).
from fastapi import Depends, HTTPException, Query, Security, status
from fastapi.security import APIKeyHeader

from ..models.schemas import validate_tenant_id
from .config import get_settings

_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# EIE-SEC-03: environments in which a direct, key-authenticated HTTP read of
# tenant-scoped data is served. This is the same set app/main.py runs the SDK
# runtime in `dev_mode` (unsigned packets, Gate-only ingress not enforced) and
# app/core/config.py permits the deterministic provider in. Outside it the node
# is signed and tenant-facing: the only authority for "which tenant" is Gate,
# carried as `packet.tenant.org_id`, and a single shared API key does not bind
# its holder to any tenant — so a direct tenant read is refused.
DIRECT_TENANT_READ_ENVIRONMENTS: frozenset[str] = frozenset({"local", "dev", "test"})

_GATE_INGRESS_REQUIRED = (
    "tenant-scoped reads are served through Gate ingress in this environment; "
    "a direct X-API-Key read is not bound to a tenant"
)


def verify_api_key(
    api_key: Annotated[str | None, Security(_header)],
) -> str:
    """Validate API key via constant-time hash comparison.

    Returns the raw API key string on success so downstream chassis code
    can use it for tenant resolution (INV-ARCH-05: tenant resolution is
    chassis-only).
    """
    if not api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing X-API-Key header")

    provided_hash = hashlib.sha256(api_key.encode()).hexdigest()

    if not hmac.compare_digest(provided_hash, get_settings().api_key_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid API key")

    return api_key


def require_direct_tenant_read(
    _api_key: Annotated[str, Depends(verify_api_key)],
) -> None:
    """Gate a tenant-scoped HTTP read behind the environment's trust posture.

    EIE-SEC-03. The API key authenticates the caller; it does not bind the
    caller to a tenant, because one key hash serves the deployment. A request
    that names its own tenant (query or body) is therefore trusted only where
    the SDK runtime itself is unsigned (DIRECT_TENANT_READ_ENVIRONMENTS).
    Everywhere else tenant identity must arrive through Gate, and the direct
    read is refused with 403 — fail closed, no silent fallback tenant.

    Use as a route or router dependency (`dependencies=[Depends(...)]`) when
    the tenant travels in the request body; use `tenant_scope` when it is a
    query parameter.
    """
    ensure_direct_tenant_read_allowed()


def ensure_direct_tenant_read_allowed() -> None:
    """The EIE-SEC-03 posture itself: 403 outside DIRECT_TENANT_READ_ENVIRONMENTS.

    The one owner of the direct-tenant-access rule. `require_direct_tenant_read`
    applies it as a FastAPI dependency; code that resolves the tenant inside a
    route body (app/api/v1/converge.py `require_direct_domain_access`) calls it
    directly, so every direct tenant route answers the same status.
    """
    if get_settings().l9_environment not in DIRECT_TENANT_READ_ENVIRONMENTS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, _GATE_INGRESS_REQUIRED)


def tenant_scope(
    tenant_id: Annotated[str, Query(..., description="Tenant identifier")],
    _gate: Annotated[None, Depends(require_direct_tenant_read)],
) -> str:
    """Resolve the tenant a direct HTTP read is scoped to.

    Composes `verify_api_key` -> `require_direct_tenant_read` -> identity
    grammar: tenant id == domain spec id == Neo4j database name, so a value
    that is not a legal Neo4j database name is rejected (400), never
    normalised. Returns the tenant id verbatim for the route to scope its
    store access by.
    """
    try:
        return validate_tenant_id(tenant_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
