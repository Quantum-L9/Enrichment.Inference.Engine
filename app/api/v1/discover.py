"""
app/api/v1/discover.py

Schema discovery and CRM scan API.

POST /api/v1/discover                      — trigger schema discovery
POST /api/v1/scan                          — CRM field scanner (Seed tier)
GET  /api/v1/proposals/{domain}            — pending schema proposals
POST /api/v1/proposals/{proposal_id}/approve — human approval
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from app.utils.safe_convert import safe_float

from ...core.auth import verify_api_key
from ...core.config import get_settings
from ...engines.handlers import handle_discover
from ...services import pg_store
from ...services.crm_field_scanner import CRMField, scan_crm_fields, scan_result_to_dict
from ...services.crm_source import DEFAULT_SAMPLE_LIMIT, MAX_SAMPLE_LIMIT, CRMSourceError
from ...services.odoo_crm_source import OdooCRMSource

logger = structlog.get_logger("api.discover")
router = APIRouter(tags=["discover"])


# ── Request / Response Models ──────────────────────────────────────────────


class DiscoverRequest(BaseModel):
    entity_id: str
    domain: str
    object_type: str
    tenant_id: str


class CRMFieldInput(BaseModel):
    name: str
    type: str
    sample_values: list[Any] | None = None
    fill_rate: float | None = None


class ScanRequest(BaseModel):
    """CRM scan request. Exactly one of ``fields`` (manual) or ``source`` (live) is required."""

    fields: list[CRMFieldInput] | None = None
    source: Literal["odoo"] | None = None
    sample_limit: int = Field(default=DEFAULT_SAMPLE_LIMIT, ge=1, le=MAX_SAMPLE_LIMIT)
    domain: str
    tenant_id: str

    @model_validator(mode="after")
    def require_fields_xor_source(self) -> ScanRequest:
        if (self.fields is None) == (self.source is None):
            raise ValueError("exactly one of 'fields' or 'source' must be supplied")
        return self


class ApprovalRequest(BaseModel):
    approved: bool
    reviewed_by: str


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post(
    "/api/v1/discover",
    dependencies=[Depends(verify_api_key)],
    summary="Trigger schema discovery for a domain entity",
)
async def discover_schema(request: DiscoverRequest) -> dict[str, Any]:
    # Delegate to the canonical schema-discovery handler, which drives the
    # SchemaDiscoveryEngine interface (enrich → engine.analyze → proposal).
    # There is no module-level `discover()`; SchemaDiscoveryEngine is the
    # canonical entry point and handle_discover is its single production caller.
    try:
        payload = {
            "entity": {"id": request.entity_id},
            "entity_id": request.entity_id,
            "object_type": request.object_type,
            "domain": request.domain,
            "objective": f"Schema discovery for domain '{request.domain}'",
        }
        return await handle_discover(tenant=request.tenant_id, payload=payload)
    except Exception as exc:
        logger.error(
            "schema_discovery_failed",
            entity_id=request.entity_id,
            domain=request.domain,
            error=str(exc),
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail="Internal error during schema discovery"
        ) from exc


def _manual_fields(inputs: list[CRMFieldInput]) -> list[CRMField]:
    return [
        CRMField(
            name=f.name,
            field_type=f.type,
            sample_values=f.sample_values or [],
            fill_rate=f.fill_rate,
        )
        for f in inputs
    ]


async def _odoo_source_fields(sample_limit: int) -> list[CRMField]:
    """Live discovery of res.partner + crm.lead via the read-only Odoo JSON-2 adapter."""
    settings = get_settings()
    if not settings.odoo_url or not settings.odoo_api_key:
        raise HTTPException(
            status_code=503,
            detail="Odoo CRM discovery requires ODOO_URL and ODOO_API_KEY",
        )
    source = OdooCRMSource(
        base_url=settings.odoo_url,
        api_key=settings.odoo_api_key,
        database=settings.odoo_db,
    )
    try:
        return await source.fields(sample_limit=sample_limit)
    except CRMSourceError as exc:
        # Messages carry provider/model/method/status only — never credentials
        # or response bodies — so they are safe to surface to the API caller.
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post(
    "/api/v1/scan",
    dependencies=[Depends(verify_api_key)],
    summary="CRM field scan — Seed tier entry point",
    responses={
        404: {"description": "Domain not found"},
        502: {"description": "Live CRM source (Odoo) failed"},
        503: {"description": "Live CRM source not configured"},
    },
)
async def scan_crm_fields_endpoint(request: ScanRequest) -> dict[str, Any]:
    # scan_crm_fields is synchronous with the (crm_fields, domain_spec) contract.
    # Resolve domain_spec from the shared converge registry; unknown domain -> 404.
    from . import converge as _converge

    domain_spec = _converge._domain_specs.get(request.domain)
    if not domain_spec:
        available = sorted(_converge._domain_specs.keys())
        raise HTTPException(
            status_code=404,
            detail=f"Domain '{request.domain}' not found. Available: {available}",
        )
    try:
        # Two intake paths (manual fields[] or live Odoo source) converge on the
        # single existing scanner — there is exactly one scanning engine.
        if request.fields is not None:
            crm_fields = _manual_fields(request.fields)
        else:
            crm_fields = await _odoo_source_fields(request.sample_limit)
        result = scan_crm_fields(crm_fields, domain_spec)
        return scan_result_to_dict(result)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("crm_scan_failed", domain=request.domain)
        raise HTTPException(status_code=500, detail="Internal error during CRM field scan") from exc


@router.get(
    "/api/v1/proposals/{domain}",
    dependencies=[Depends(verify_api_key)],
    summary="Get pending schema proposals for a domain",
)
async def get_proposals(
    domain: str,
    tenant_id: Annotated[str, Query(..., description="Tenant identifier")],
) -> list[dict[str, Any]]:
    proposals = await pg_store.get_pending_schema_proposals(tenant_id=tenant_id, domain=domain)
    return [
        {
            "id": str(p.id),
            "field_name": p.field_name,
            "field_type": p.field_type,
            "source": p.source,
            "fill_rate": safe_float(p.fill_rate),
            "avg_confidence": safe_float(p.avg_confidence),
            "sample_values": p.sample_values,
            "proposed_gate": p.proposed_gate,
            "proposed_scoring_dimension": p.proposed_scoring_dimension,
            "yaml_diff": p.yaml_diff,
            "approval_status": p.approval_status,
            "created_at": p.created_at.isoformat(),
        }
        for p in proposals
    ]


@router.post(
    "/api/v1/proposals/{proposal_id}/approve",
    dependencies=[Depends(verify_api_key)],
    summary="Approve or reject a schema proposal",
)
async def approve_proposal(
    proposal_id: uuid.UUID,
    request: ApprovalRequest,
) -> dict[str, str]:
    await pg_store.approve_schema_proposal(
        proposal_id=proposal_id,
        reviewed_by=request.reviewed_by,
        approved=request.approved,
    )
    status = "approved" if request.approved else "rejected"
    logger.info(
        "schema_proposal_reviewed",
        proposal_id=str(proposal_id),
        status=status,
        reviewed_by=request.reviewed_by,
    )
    return {"status": status, "proposal_id": str(proposal_id)}
