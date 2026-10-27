"""
app/api/v1/fields.py

Field-level confidence and provenance API.

GET  /api/v1/fields/{entity_id}                      — field map + confidence
GET  /api/v1/fields/{entity_id}/{field_name}/history — confidence time-series
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.utils.safe_convert import safe_float

from ...core.auth import tenant_scope, verify_api_key
from ...core.config import Settings, get_settings
from ...services.result_store import ResultStore

logger = structlog.get_logger("api.fields")
router = APIRouter(prefix="/api/v1/fields", tags=["fields"])


class FieldEntry(BaseModel):
    value: Any
    confidence: float
    source: str
    pass_number: int


class FieldsResponse(BaseModel):
    entity_id: str
    fields: dict[str, FieldEntry]
    avg_confidence: float
    coverage_ratio: float
    last_enriched_at: datetime | None


@router.get(
    "/{entity_id}",
    response_model=FieldsResponse,
    dependencies=[Depends(verify_api_key)],
    summary="Get field confidence map for an entity",
)
async def get_field_confidence_map(
    entity_id: str,
    # EIE-SEC-03: `tenant_id` stays a required query parameter on the wire;
    # tenant_scope binds it (key -> environment trust -> identity grammar).
    tenant_id: Annotated[str, Depends(tenant_scope)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> FieldsResponse:
    store = ResultStore(tenant_id=tenant_id)
    latest = await store.get_latest_for_entity(entity_id)
    if latest is None:
        raise HTTPException(
            status_code=404,
            detail=f"No enrichment found for entity '{entity_id}' in tenant '{tenant_id}'",
        )

    field_entries: dict[str, FieldEntry] = {}
    total_confidence = 0.0

    for field_name, value in latest.fields.items():
        history = await store.get_field_confidence_history(
            entity_id, field_name, enrichment_result_id=latest.id
        )
        if history:
            latest_entry = history[-1]
            conf = latest_entry["confidence"]
            source = latest_entry["source"]
            pass_num = latest_entry["pass_number"]
        else:
            conf = safe_float(latest.confidence)
            source = "enrichment"
            pass_num = latest.pass_count

        field_entries[field_name] = FieldEntry(
            value=value,
            confidence=conf,
            source=source,
            pass_number=pass_num,
        )
        total_confidence += conf

    field_count = len(field_entries)
    avg_conf = total_confidence / field_count if field_count else 0.0

    coverage_ratio = _coverage_ratio(latest.fields, getattr(latest, "feature_vector", None))

    return FieldsResponse(
        entity_id=entity_id,
        fields=field_entries,
        avg_confidence=round(avg_conf, 4),
        coverage_ratio=round(coverage_ratio, 4),
        last_enriched_at=latest.created_at,
    )


def _is_populated(value: Any) -> bool:
    return value not in (None, "", [], {})


def _coverage_ratio(fields: dict[str, Any], feature_vector: dict[str, Any] | None) -> float:
    """Share of the run's target fields that hold a value (F-042).

    The denominator is the requested schema when the run recorded one
    (``feature_vector["requested_fields"]``), otherwise every field the run
    attempted (tracked, scored, or returned). It is never the returned field
    count itself, which made coverage 1.0 by construction.
    """
    fv = feature_vector or {}
    requested = fv.get("requested_fields")
    if isinstance(requested, list) and requested:
        targets = {str(name) for name in requested}
    else:
        targets = set(fields)
        tracking = fv.get("confidence_tracking")
        if isinstance(tracking, dict) and isinstance(tracking.get("fields"), dict):
            targets |= set(tracking["fields"])
        if isinstance(fv.get("per_field_confidence"), dict):
            targets |= set(fv["per_field_confidence"])
    if not targets:
        return 0.0
    filled = sum(1 for name in targets if _is_populated(fields.get(name)))
    return filled / len(targets)


@router.get(
    "/{entity_id}/{field_name}/history",
    dependencies=[Depends(verify_api_key)],
    summary="Get confidence time-series for a specific field",
)
async def get_field_confidence_history(
    entity_id: str,
    field_name: str,
    tenant_id: Annotated[str, Depends(tenant_scope)],
) -> list[dict]:
    store = ResultStore(tenant_id=tenant_id)
    history = await store.get_field_confidence_history(entity_id, field_name)
    if not history:
        raise HTTPException(
            status_code=404,
            detail=f"No confidence history for field '{field_name}' on entity '{entity_id}'",
        )
    return history
