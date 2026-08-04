"""
app/api/v1/converge.py
Convergence API router.

Integration fixes applied (PR#22 merge pass):
    GAP-3: configure() injected at startup (main.py lifespan).
    GAP-4: converge_single delegates to convergence_controller.run_convergence_loop().
    GAP-9: converge_batch accepts inline entity list and returns real counts.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.utils.safe_convert import safe_float

from ...core.auth import verify_api_key
from ...core.config import Settings, get_settings
from ...engines.convergence.loop_state import LoopState, LoopStateStore, LoopStatus
from ...engines.convergence.schema_proposer import ApprovalDecision
from ...engines.convergence.schema_proposer import apply as apply_proposals
from ...engines.convergence.schema_proposer import propose as propose_schema
from ...models.schemas import EnrichRequest
from ...services.crm_field_scanner import (
    CRMField,
    DiscoveryReport,
    generate_discovery_report,
    scan_crm_fields,
)
from ...services.enrichment_profile import EnrichmentProfile, ProfileRegistry

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["convergence"], dependencies=[Depends(verify_api_key)])

_state_store: LoopStateStore | None = None
_profile_registry: ProfileRegistry | None = None
_domain_specs: dict[str, dict[str, Any]] = {}
_kb_resolver: Any = None
_idem_store: Any = None


def configure(
    state_store: LoopStateStore,
    profile_registry: ProfileRegistry,
    domain_specs: dict[str, dict[str, Any]],
    kb_resolver: Any = None,
    idem_store: Any = None,
) -> None:
    """GAP-3: Called once from main.py lifespan to inject all dependencies."""
    global _state_store, _profile_registry, _domain_specs, _kb_resolver, _idem_store
    _state_store = state_store
    _profile_registry = profile_registry
    _domain_specs = domain_specs
    _kb_resolver = kb_resolver
    _idem_store = idem_store


def _get_state_store() -> LoopStateStore:
    if _state_store is None:
        raise HTTPException(status_code=503, detail="State store not configured")
    return _state_store


def _get_profile_registry() -> ProfileRegistry:
    if _profile_registry is None:
        raise HTTPException(status_code=503, detail="Profile registry not configured")
    return _profile_registry


class ConvergeRequestBody(BaseModel):
    entity: dict[str, Any] = Field(...)
    object_type: str = "Account"
    domain: str = ""
    objective: str = "Full entity enrichment and inference"
    max_passes: int = Field(default=5, ge=1, le=20)
    max_budget_tokens: int = Field(default=50000, ge=1000)
    convergence_threshold: float = Field(default=2.0, ge=0.0)
    approval_mode: str = "auto"
    idempotency_key: str | None = None


class BatchConvergeRequestBody(BaseModel):
    profile_name: str | None = None
    inline_profile: EnrichmentProfile | None = None
    domain: str = ""
    entity_ids: list[str] = Field(default_factory=list)
    entities: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Inline entity list for immediate batch processing (GAP-9)",
    )


class ScanRequestBody(BaseModel):
    fields: list[CRMField]
    domain: str = ""


class ApproveRequestBody(BaseModel):
    decisions: list[ApprovalDecision]


class ConvergeSingleResponse(BaseModel):
    run_id: str
    status: str
    passes_completed: int = 0
    fields_discovered: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0
    convergence_reason: str = ""


class BatchConvergeResponse(BaseModel):
    entities_selected: int = 0
    entities_processed: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    profile_used: str = ""
    run_ids: list[str] = Field(default_factory=list)


@router.post("/converge", response_model=ConvergeSingleResponse)
async def converge_single(
    body: ConvergeRequestBody,
    settings: Settings = Depends(get_settings),
) -> ConvergeSingleResponse:
    """
    GAP-4: Run full multi-pass convergence via convergence_controller.run_convergence_loop().
    """
    from ...engines.convergence.convergence_config import ConvergenceConfig
    from ...engines.convergence_controller import run_convergence_loop

    store = _get_state_store()

    state = LoopState(
        entity_id=str(body.entity.get("id", body.entity.get("Name", "unknown"))),
        domain=body.domain,
        status=LoopStatus.RUNNING,
    )
    await store.save(state)

    enrich_request = EnrichRequest(
        entity=body.entity,
        object_type=body.object_type,
        objective=body.objective,
        idempotency_key=body.idempotency_key,
    )
    convergence_cfg = ConvergenceConfig(
        max_passes=body.max_passes,
        min_improvement_delta=body.convergence_threshold,
        confidence_threshold=min(body.convergence_threshold / 10.0, 1.0),
    )

    try:
        conv_response = await run_convergence_loop(
            request=enrich_request,
            settings=settings,
            kb_resolver=_kb_resolver,
            idem_store=_idem_store,
            convergence_config=convergence_cfg,
        )
    except Exception as exc:
        state.status = LoopStatus.FAILED
        state.failure_reason = str(exc)
        await store.save(state)
        logger.error("convergence_loop_failed", run_id=state.run_id, error=str(exc))
        raise HTTPException(status_code=500, detail="Internal error during convergence") from exc

    state.accumulated_fields = conv_response.fields or {}
    state.current_pass = conv_response.pass_count
    state.status = LoopStatus.CONVERGED if conv_response.state == "completed" else LoopStatus.FAILED
    await store.save(state)

    convergence_reason = getattr(conv_response, "convergence_reason", "completed")
    cost_usd = safe_float(conv_response.tokens_used) * settings.token_rate_usd_per_1k / 1000.0

    return ConvergeSingleResponse(
        run_id=state.run_id,
        status=state.status.value,
        passes_completed=conv_response.pass_count,
        fields_discovered=len(state.accumulated_fields),
        tokens_used=conv_response.tokens_used,
        cost_usd=cost_usd,
        convergence_reason=convergence_reason,
    )


@router.post("/converge/batch", response_model=BatchConvergeResponse)
async def converge_batch(
    body: BatchConvergeRequestBody,
    settings: Settings = Depends(get_settings),
) -> BatchConvergeResponse:
    """
    GAP-9: Batch convergence. Accepts inline entity list; returns actual counts.
    """
    from ...engines.convergence.convergence_config import ConvergenceConfig
    from ...engines.convergence_controller import run_convergence_loop

    registry = _get_profile_registry()
    store = _get_state_store()

    profile: EnrichmentProfile | None = None
    if body.inline_profile:
        profile = body.inline_profile
    elif body.profile_name:
        profile = registry.get(body.profile_name)
    if profile is None:
        raise HTTPException(status_code=400, detail="No profile specified or found")

    entities = body.entities
    if not entities:
        return BatchConvergeResponse(
            entities_selected=0,
            entities_processed=0,
            total_tokens=0,
            total_cost_usd=0.0,
            profile_used=profile.profile_name,
            run_ids=[],
        )

    convergence_cfg = ConvergenceConfig(max_passes=profile.max_passes)
    run_ids: list[str] = []
    total_tokens = 0
    processed = 0

    for entity_dict in entities[: profile.batch_size]:
        enrich_req = EnrichRequest(
            entity=entity_dict,
            object_type=entity_dict.get("object_type", "Account"),
            objective=f"Batch convergence via profile: {profile.profile_name}",
        )
        state = LoopState(
            entity_id=str(entity_dict.get("id", entity_dict.get("Name", "unknown"))),
            domain=body.domain,
            status=LoopStatus.RUNNING,
        )
        await store.save(state)
        try:
            resp = await run_convergence_loop(
                request=enrich_req,
                settings=settings,
                kb_resolver=_kb_resolver,
                idem_store=_idem_store,
                convergence_config=convergence_cfg,
            )
            state.accumulated_fields = resp.fields or {}
            state.current_pass = resp.pass_count
            state.status = LoopStatus.CONVERGED if resp.state == "completed" else LoopStatus.FAILED
            total_tokens += resp.tokens_used
            processed += 1
        except Exception as exc:
            state.status = LoopStatus.FAILED
            state.failure_reason = str(exc)
            logger.warning(
                "converge_batch.entity_failed",
                extra={"error": str(exc)},
            )
        await store.save(state)
        run_ids.append(state.run_id)

    total_cost_usd = total_tokens * settings.token_rate_usd_per_1k / 1000.0

    return BatchConvergeResponse(
        entities_selected=len(entities),
        entities_processed=processed,
        total_tokens=total_tokens,
        total_cost_usd=total_cost_usd,
        profile_used=profile.profile_name,
        run_ids=run_ids,
    )


@router.get("/converge/{run_id}")
async def get_convergence_status(run_id: str) -> dict[str, Any]:
    store = _get_state_store()
    state = await store.load(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return {
        "run_id": state.run_id,
        "entity_id": state.entity_id,
        "domain": state.domain,
        "status": state.status.value,
        "current_pass": state.current_pass,
        "fields_accumulated": len(state.accumulated_fields),
        "cost": state.cost_summary.model_dump(),
        "created_at": state.created_at.isoformat(),
        "updated_at": state.updated_at.isoformat(),
    }


@router.post("/converge/{run_id}/approve")
async def approve_proposals(run_id: str, body: ApproveRequestBody) -> dict[str, Any]:
    store = _get_state_store()
    state = await store.load(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    if state.status != LoopStatus.HUMAN_HOLD:
        raise HTTPException(
            status_code=409, detail=f"Run is {state.status.value}, not awaiting approval"
        )
    domain_spec = _domain_specs.get(state.domain, {})
    if not domain_spec:
        raise HTTPException(status_code=404, detail=f"Domain spec for '{state.domain}' not found")
    proposal_set = propose_schema(
        batch_results=[
            {
                "final_fields": state.accumulated_fields,
                "final_field_confidences": state.accumulated_confidences,
            }
        ],
        current_yaml=domain_spec,
        domain=state.domain,
    )
    updated_yaml = apply_proposals(domain_spec, body.decisions, proposal_set)
    _domain_specs[state.domain] = updated_yaml
    approved_count = sum(1 for d in body.decisions if d.approved)
    state.status = LoopStatus.CONVERGED
    await store.save(state)
    return {
        "run_id": run_id,
        "approved": approved_count,
        "rejected": len(body.decisions) - approved_count,
        "new_version": updated_yaml.get("version", "unknown"),
    }


@router.get("/converge/proposals/{domain}")
async def get_pending_proposals(domain: str) -> dict[str, Any]:
    store = _get_state_store()
    active_runs = await store.list_active(domain=domain)
    held_runs = [r for r in active_runs if r.status == LoopStatus.HUMAN_HOLD]
    domain_spec = _domain_specs.get(domain, {})
    proposals: list[dict[str, Any]] = []
    for run in held_runs:
        ps = propose_schema(
            batch_results=[
                {
                    "final_fields": run.accumulated_fields,
                    "final_field_confidences": run.accumulated_confidences,
                }
            ],
            current_yaml=domain_spec,
            domain=domain,
        )
        proposals.append(
            {"run_id": run.run_id, "entity_id": run.entity_id, "proposal": ps.model_dump()}
        )
    return {"domain": domain, "pending_count": len(proposals), "proposals": proposals}


@router.post("/scan", response_model=DiscoveryReport)
async def scan_crm(body: ScanRequestBody) -> DiscoveryReport:
    domain_spec = _domain_specs.get(body.domain)
    if not domain_spec:
        available = list(_domain_specs.keys())
        raise HTTPException(
            status_code=400, detail=f"Domain '{body.domain}' not found. Available: {available}"
        )
    scan_result = scan_crm_fields(body.fields, domain_spec)
    return generate_discovery_report(scan_result, domain_spec)
