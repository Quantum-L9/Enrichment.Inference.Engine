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

# L9-node: enrichment-inference-engine
# L9-layer: chassis
# L9-contract-version: 1.0.0
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Annotated

# FastAPI Security imports are PERMITTED in chassis auth modules.
# Engine code must never replicate these imports (INV-ARCH-03).
from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from .config import get_settings

_header = APIKeyHeader(name="X-API-Key", auto_error=False)


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
