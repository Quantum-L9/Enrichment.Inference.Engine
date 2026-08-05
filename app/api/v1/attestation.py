from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, HTTPException

from app.services.runtime_attestation import build_runtime_attestation

router = APIRouter(prefix="/v1", tags=["attestation"])
logger = structlog.get_logger(__name__)


@router.get("/attestation")
def get_runtime_attestation() -> dict[str, Any]:
    try:
        return build_runtime_attestation()
    except Exception as exc:
        logger.error("runtime_attestation_failed", error=str(exc), exc_info=True)
        raise HTTPException(
            status_code=500, detail="Internal error during runtime attestation"
        ) from exc
