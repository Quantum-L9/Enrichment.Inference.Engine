"""
app/api/v1/discover.py

Schema discovery and CRM scan API.

POST /api/v1/discover                      — trigger schema discovery
POST /api/v1/scan                          — CRM field scanner (Seed tier)
GET  /api/v1/proposals/{domain}            — pending schema proposals
POST /api/v1/proposals/{proposal_id}/approve — human approval
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Annotated, Any

import structlog
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.utils.safe_convert import safe_float

from ...core.auth import verify_api_key
from ...core.config import Settings, get_settings
from ...engines.handlers import handle_discover
from ...services import pg_store
from ...services.crm_field_scanner import (
    CRMField,
    scan_result_to_dict,
)
from ...services.crm_field_scanner import (
    scan_crm_fields as run_crm_field_scan,
)

logger = structlog.get_logger("api.discover")
router = APIRouter(tags=["discover"])

# Domain identifiers are path segments under settings.domains_dir; restrict them
# to a safe character set so a request value can never traverse the filesystem.
_DOMAIN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Raw domain-spec YAML is static; cache per domain to keep file I/O off the
# request path after first load (mirrors DomainYamlReader's caching).
_DOMAIN_SPEC_CACHE: dict[str, dict[str, Any]] = {}


def _resolve_domain_spec(domain: str, settings: Settings) -> dict[str, Any]:
    """Resolve a domain id to its raw domain-spec dict.

    Reads the canonical ``{domains_dir}/{domain}/spec.yaml`` (falling back to
    ``{domains_dir}/{domain}.yaml``), the same layout DomainYamlReader uses.
    Returns the raw mapping expected by ``scan_crm_fields``. Raises HTTP 404 for
    an unknown or malformed domain id.
    """
    if not _DOMAIN_ID_RE.match(domain):
        raise HTTPException(status_code=404, detail=f"Domain '{domain}' not found")
    if domain in _DOMAIN_SPEC_CACHE:
        return _DOMAIN_SPEC_CACHE[domain]

    root = Path(settings.domains_dir)
    spec_path = root / domain / "spec.yaml"
    if not spec_path.exists():
        spec_path = root / f"{domain}.yaml"
    if not spec_path.exists():
        raise HTTPException(status_code=404, detail=f"Domain '{domain}' not found")

    with open(spec_path) as fh:
        spec = yaml.safe_load(fh) or {}
    _DOMAIN_SPEC_CACHE[domain] = spec
    return spec


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
) -> dict[str, Any]:
    # Delegate to the canonical "discover" action handler (enrich + schema
    # proposal); it is the same implementation registered for the chassis
    # `discover` action. The request's entity id/domain/object_type are mapped
    # onto the EnrichRequest payload the handler validates.
    payload: dict[str, Any] = {
        "entity": {"id": request.entity_id},
        "object_type": request.object_type,
        "objective": f"Discover schema fields for domain '{request.domain}'",
        "kb_context": request.domain,
    }
    try:
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


@router.post(
    "/api/v1/scan",
    dependencies=[Depends(verify_api_key)],
    summary="CRM field scan — Seed tier entry point",
)
async def scan_crm_fields(
    request: ScanRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    # Resolve the domain to its raw spec (404 if unknown), then run the
    # canonical synchronous field scanner with its real (crm_fields, domain_spec)
    # signature. scan_crm_fields is pure in-memory field mapping (no blocking
    # I/O), so it is safe to call directly on the event loop.
    domain_spec = _resolve_domain_spec(request.domain, settings)
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
        scan_result = run_crm_field_scan(crm_fields, domain_spec)
    except Exception as exc:
        logger.error(
            "crm_scan_failed",
            domain=request.domain,
            tenant_id=request.tenant_id,
            error=str(exc),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Internal error during CRM field scan") from exc
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
