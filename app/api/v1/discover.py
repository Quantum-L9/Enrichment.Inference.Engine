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
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ...core.auth import verify_api_key
from ...core.config import Settings, get_settings
from ...services import pg_store

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
    fields: list[CRMFieldInput]
    domain: str
    tenant_id: str


class ApprovalRequest(BaseModel):
    approved: bool
    reviewed_by: str


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post(
    "/api/v1/discover",
    dependencies=[Depends(verify_api_key)],
    summary="Trigger schema discovery for a domain entity",
)
async def discover_schema(
    request: DiscoverRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    # Schema discovery is driven by convergence output (enriched + inferred
    # fields), not by a bare entity trigger: SchemaDiscoveryEngine.analyze()
    # (app/engines/schema_discovery.py) requires enriched_fields/inferred_fields/
    # confidence_map, and there is no entity-triggered discovery entrypoint.
    # Respond honestly rather than importing a symbol that does not exist.
    logger.info(
        "schema_discovery_not_implemented",
        entity_id=request.entity_id,
        domain=request.domain,
    )
    raise HTTPException(
        status_code=501,
        detail=(
            "Entity-triggered schema discovery is not implemented. Schema "
            "proposals are produced by the convergence pipeline, which invokes "
            "SchemaDiscoveryEngine on enriched/inferred fields."
        ),
    )


@router.post(
    "/api/v1/scan",
    dependencies=[Depends(verify_api_key)],
    summary="CRM field scan — Seed tier entry point",
)
async def scan_crm_fields(
    request: ScanRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    from ...services.crm_field_scanner import CRMField, scan_result_to_dict
    from ...services.crm_field_scanner import scan_crm_fields as _scan
    from . import converge

    # Domain specs are held in the shared runtime registry populated at startup
    # (see app/main.py -> configure_converge). Accessed via the module attribute
    # so the current registry (not a stale import-time copy) is read.
    domain_spec = converge._domain_specs.get(request.domain)
    if not domain_spec:
        available = sorted(converge._domain_specs.keys())
        raise HTTPException(
            status_code=400,
            detail=f"Domain '{request.domain}' not found. Available: {available}",
        )

    crm_fields = [
        CRMField(
            name=f.name,
            field_type=f.type,
            sample_values=f.sample_values or [],
            fill_rate=f.fill_rate,
        )
        for f in request.fields
    ]

    try:
        scan_result = _scan(crm_fields, domain_spec)
    except Exception as exc:
        logger.error("crm_scan_failed", domain=request.domain, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return scan_result_to_dict(scan_result)


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
            "fill_rate": float(p.fill_rate),
            "avg_confidence": float(p.avg_confidence),
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
