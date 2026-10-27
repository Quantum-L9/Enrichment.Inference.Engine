"""
app/api/v1/converge.py
Convergence API router.

Integration fixes applied (PR#22 merge pass):
    GAP-3: configure() injected at startup (main.py lifespan).
    GAP-4: converge_single delegates to convergence_controller.run_convergence_loop().
    GAP-9: converge_batch accepts inline entity list and returns real counts.

Domain registry (EIE-IDENT-02 / EIE-IDENT-03):
    `_domain_specs` is the one in-process registry of domain specs, keyed by
    tenant id. The identity contract (2026-10-22) is tenant id == domain spec
    id == Neo4j database name: one customer, one `domains/<tenant>/spec.yaml`,
    one versioned spec, one database. `build_domain_spec_registry` loads it at
    startup; `hydrate_approved_revisions` replays the approved rows of the
    `schema_proposals` table onto it; `approve_proposals` persists a new
    revision there before the registry takes it.

Direct tenant access (F-247-4 / F-247-5):
    The scan, approval and proposal routes read or revise one tenant's spec,
    and the X-API-Key binds its holder to no tenant (one key hash serves the
    deployment). `require_direct_domain_access` therefore serves them only in
    the unsigned runtimes (local/dev/test) and answers 403 everywhere else.
    Both scan routes resolve through `get_domain_spec`, which applies it, so
    `POST /v1/scan` and `POST /api/v1/scan` answer the same status.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

import structlog
import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.utils.safe_convert import safe_float

from ...core.auth import ensure_direct_tenant_read_allowed, verify_api_key
from ...core.config import Settings, get_settings
from ...engines.convergence.loop_state import LoopState, LoopStateStore, LoopStatus
from ...engines.convergence.schema_proposer import (
    ApprovalDecision,
    FieldProposal,
    GateProposal,
    SchemaProposalSet,
    ScoringDimensionProposal,
    applied_field_names,
    revision_from_approved,
)
from ...engines.convergence.schema_proposer import apply as apply_proposals
from ...engines.convergence.schema_proposer import propose as propose_schema
from ...engines.domain_yaml_reader import (
    TENANT_ID_PATTERN,
    TENANT_ID_RESERVED_PREFIX,
    DomainYamlReader,
    declared_spec_identity,
    is_legal_tenant_id,
)
from ...models.loop_schemas import ApprovalMode
from ...models.schemas import EnrichRequest, validate_tenant_id
from ...services import pg_store
from ...services.crm_field_scanner import (
    CRMField,
    DiscoveryReport,
    ScanResult,
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
# One asyncio.Lock per domain, held across propose -> apply -> persist ->
# publish of an approval. Two concurrent approvals for the same domain used to
# compute `updated_yaml` from the same snapshot, so the last writer silently
# dropped the other's approved fields until the next restart replayed them.
# Process-local, like the registry it protects (one in-process registry).
_domain_locks: dict[str, asyncio.Lock] = {}

# The identity grammar lives with the reader (app/engines/domain_yaml_reader.py)
# so the filesystem check and the registry check are one function; the name is
# re-exported here for the API-side callers and tests that pin it.
__all__ = ["is_legal_tenant_id"]

# F-247-4 / F-247-5: direct, key-authenticated reads and revisions of one
# tenant's spec follow the node's one direct-tenant-access posture (EIE-SEC-03,
# app/core/auth.py `ensure_direct_tenant_read_allowed`): served only in the
# unsigned runtimes, 403 everywhere else -- fail closed, no fallback tenant.
# OpenAPI entry for every route guarded by require_direct_domain_access.
_DIRECT_DOMAIN_ACCESS_RESPONSE: dict[str, Any] = {
    "description": "Direct tenant-scoped access is refused outside local/dev/test"
}


class SchemaRevisionHydrateError(RuntimeError):
    """The approved revisions of a registered tenant could not be read at startup."""


def require_direct_domain_access() -> None:
    """Refuse a direct tenant-scoped spec read or revision outside the unsigned runtimes.

    Called first by :func:`get_domain_spec` (so both scan routes share it), by
    :func:`approve_proposals` and by :func:`get_pending_proposals`: before any
    run, spec or registry listing is read, so a refused caller learns nothing
    about which tenants exist. The rule is `ensure_direct_tenant_read_allowed`,
    the same one `require_direct_tenant_read` applies to `/fields`,
    `/proposals`, `/discover` and `/score`.
    """
    ensure_direct_tenant_read_allowed()


def _domain_lock(domain: str) -> asyncio.Lock:
    lock = _domain_locks.get(domain)
    if lock is None:
        lock = _domain_locks[domain] = asyncio.Lock()
    return lock


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


def build_domain_spec_registry(reader: DomainYamlReader) -> dict[str, dict[str, Any]]:
    """One spec per tenant, keyed by tenant id, read from the domains root.

    EIE-IDENT-02. Only ``domains/<tenant>/spec.yaml`` names a tenant: a flat
    ``domains/<name>.yaml`` is refused with a warning naming the file, so a
    stray ``acme.bak.yaml`` never registers tenant ``acme.bak`` and an
    ``acme.yaml`` beside ``acme/`` is not silently shadowed. The directory name
    IS the tenant id, the domain spec id and the Neo4j database name, so it has
    to be a legal database name (:func:`is_legal_tenant_id`). A directory that
    is not, a spec that cannot be read, or a spec whose declared id
    (:func:`declared_spec_identity`) disagrees with its directory is refused
    with a warning naming it -- never registered under an identity it does not
    hold, and never mapped to one. Template provenance (``metadata.template.*``)
    is not consulted: it never selects a spec.
    """
    registry: dict[str, dict[str, Any]] = {}
    for flat_file in sorted(reader.list_flat_specs()):
        logger.warning(
            "domain_registry_refused_flat_spec",
            file=flat_file,
            rule="a tenant is a directory: domains/<tenant>/spec.yaml",
        )
    for domain_id in sorted(reader.list_directory_domains()):
        if not is_legal_tenant_id(domain_id):
            logger.warning(
                "domain_registry_refused_illegal_tenant_id",
                domain_id=domain_id,
                pattern=TENANT_ID_PATTERN.pattern,
                reserved_prefix=TENANT_ID_RESERVED_PREFIX,
            )
            continue
        # AttributeError / TypeError / KeyError: valid YAML with the wrong shape
        # (a list root, `ontology.nodes` as a mapping) raises inside
        # DomainYamlReader._parse. One tenant's broken spec refuses that tenant;
        # it does not fail the whole registry.
        try:
            spec = reader.load(domain_id).raw_spec
        except (
            FileNotFoundError,
            ValueError,
            OSError,
            yaml.YAMLError,
            AttributeError,
            TypeError,
            KeyError,
        ) as exc:
            logger.warning(
                "domain_registry_spec_unreadable",
                domain_id=domain_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        if not isinstance(spec, dict):
            logger.warning("domain_registry_spec_not_a_mapping", domain_id=domain_id)
            continue
        declared = declared_spec_identity(spec)[0]
        if declared is not None and declared != domain_id:
            logger.warning(
                "domain_registry_refused_identity_mismatch",
                domain_id=domain_id,
                declared_id=declared,
            )
            continue
        registry[domain_id] = spec
    logger.info("domain_registry_built", domains=sorted(registry))
    return registry


def registered_domains() -> list[str]:
    """Tenant ids the registry currently serves (readiness reads this)."""
    return sorted(_domain_specs)


def _record_proposals(
    records: list[Any],
) -> tuple[list[FieldProposal], list[GateProposal], list[ScoringDimensionProposal]]:
    """Rebuild proposal objects from persisted ``schema_proposals`` rows."""
    fields: list[FieldProposal] = []
    gates: list[GateProposal] = []
    dims: list[ScoringDimensionProposal] = []
    for record in records:
        fields.append(
            FieldProposal(
                field_name=record.field_name,
                field_type=record.field_type or "string",
                source=record.source or "enrichment",
                fill_rate=safe_float(record.fill_rate),
                avg_confidence=safe_float(record.avg_confidence),
                sample_values=list(record.sample_values or []),
                value_distribution=dict(record.value_distribution or {}),
            )
        )
        if record.proposed_gate:
            gates.append(GateProposal(field_name=record.field_name, gate_type=record.proposed_gate))
        if record.proposed_scoring_dimension:
            dims.append(
                ScoringDimensionProposal(
                    field_name=record.field_name,
                    dimension_type=record.proposed_scoring_dimension,
                )
            )
    return fields, gates, dims


def apply_approved_records(spec: dict[str, Any], records: list[Any]) -> dict[str, Any]:
    """Replay approved ``schema_proposals`` rows onto ``spec``, batch by batch.

    Each ``batch_run_id`` was one approval, so each is one revision and one
    version bump, applied in the order the rows were created.
    """
    batches: OrderedDict[str, list[Any]] = OrderedDict()
    for record in records:
        batches.setdefault(record.batch_run_id, []).append(record)
    revised = spec
    for batch in batches.values():
        fields, gates, dims = _record_proposals(batch)
        revision = revision_from_approved(revised, fields=fields, gates=gates, dims=dims)
        decisions = [ApprovalDecision(field_name=f.field_name, approved=True) for f in fields]
        revised = apply_proposals(revised, decisions, revision)
    return revised


async def hydrate_approved_revisions() -> dict[str, int]:
    """Read each registered domain's approved revision into the registry.

    EIE-IDENT-03 read side, run once at startup after the file-backed registry
    is built. Returns ``{tenant_id: approved_rows_applied}``. A store that is
    not reachable stops the walk (instead of timing out once per domain),
    leaves that tenant at its file-backed revision, and raises
    :class:`SchemaRevisionHydrateError`: app/main.py records the registry as
    ``hydrate_failed`` and readiness answers 503, so a node serving a rolled
    back revision is never Ready (F-247-10).
    """
    applied: dict[str, int] = {}
    for domain in registered_domains():
        try:
            records = await pg_store.get_approved_schema_proposals(tenant_id=domain, domain=domain)
        except Exception as exc:
            logger.warning(
                "schema_revision_hydrate_unavailable",
                domain=domain,
                error=f"{type(exc).__name__}: {exc}",
            )
            msg = f"approved schema revisions of tenant {domain!r} could not be read"
            raise SchemaRevisionHydrateError(msg) from exc
        if not records:
            continue
        _domain_specs[domain] = apply_approved_records(_domain_specs[domain], records)
        applied[domain] = len(records)
        logger.info(
            "schema_revision_hydrated",
            domain=domain,
            approved_rows=len(records),
            version=_domain_specs[domain].get("version"),
        )
    return applied


async def persist_approved_revision(
    *,
    tenant_id: str,
    domain: str,
    batch_run_id: str,
    proposal_set: SchemaProposalSet,
    decisions: list[ApprovalDecision],
    reviewed_by: str,
) -> int:
    """Write every decided proposal of one approval to ``schema_proposals``.

    EIE-IDENT-03 write side. The whole batch goes through
    :func:`pg_store.persist_schema_proposal_batch` in ONE transaction: each
    decided field is upserted (keyed on tenant, domain, batch, field) and
    stamped approved or rejected, and a failure on any row leaves zero rows of
    this batch durable. Rejections are persisted too: they are the audit trail
    of what a reviewer declined. Returns the number of rows written; raises
    whatever the store raised so the caller can refuse to apply an
    unpersisted revision.
    """
    decided = {d.field_name: d.approved for d in decisions}
    gate_by_field = {g.field_name: g.gate_type for g in proposal_set.proposed_gates}
    dim_by_field = {
        d.field_name: d.dimension_type for d in proposal_set.proposed_scoring_dimensions
    }
    rows = [
        {
            "field_name": fp.field_name,
            "field_type": fp.field_type,
            "source": fp.source,
            "fill_rate": fp.fill_rate,
            "avg_confidence": fp.avg_confidence,
            "sample_values": fp.sample_values,
            "value_distribution": fp.value_distribution,
            "proposed_gate": gate_by_field.get(fp.field_name),
            "proposed_scoring_dimension": dim_by_field.get(fp.field_name),
            "yaml_diff": proposal_set.yaml_diff,
            "approved": decided[fp.field_name],
        }
        for fp in proposal_set.proposed_fields
        if fp.field_name in decided
    ]
    if not rows:
        return 0
    records = await pg_store.persist_schema_proposal_batch(
        tenant_id=tenant_id,
        domain=domain,
        batch_run_id=batch_run_id,
        reviewed_by=reviewed_by,
        decisions=rows,
    )
    return len(records)


def _get_state_store() -> LoopStateStore:
    if _state_store is None:
        raise HTTPException(status_code=503, detail="State store not configured")
    return _state_store


def _get_profile_registry() -> ProfileRegistry:
    if _profile_registry is None:
        raise HTTPException(status_code=503, detail="Profile registry not configured")
    return _profile_registry


def get_domain_spec(domain: str) -> dict[str, Any]:
    """Resolve a configured domain spec from the injected registry.

    The single resolution path for CRM scans: ``POST /v1/scan`` (this router)
    and ``POST /api/v1/scan`` (app/api/v1/discover.py) both call it, so they
    share one tenant posture (:func:`require_direct_domain_access`, 403) and an
    unknown domain is HTTP 404 on both -- the status the OpenAPI contract
    documents for ``/api/v1/scan`` -- with the same detail. The registry is
    populated once at startup via :func:`configure` (main.py lifespan).
    """
    require_direct_domain_access()
    domain_spec = _domain_specs.get(domain)
    if not domain_spec:
        available = sorted(_domain_specs)
        raise HTTPException(
            status_code=404, detail=f"Domain '{domain}' not found. Available: {available}"
        )
    return domain_spec


def scan_registered_domain(
    domain: str, crm_fields: list[CRMField]
) -> tuple[ScanResult, dict[str, Any]]:
    """Scan ``crm_fields`` against the registered spec of ``domain``, under its identity.

    Both scan routes call it. The result carries the registry key as its
    ``domain_id`` (tenant id == domain spec id) and the spec's declared version
    (:func:`declared_spec_identity`), so two tenants sharing one ontology scan
    to distinct ids and hashes (F-247-3). Returns the scan and the spec.
    """
    domain_spec = get_domain_spec(domain)
    version = declared_spec_identity(domain_spec)[1]
    result = scan_crm_fields(crm_fields, domain_spec, domain_id=domain, domain_version=version)
    return result, domain_spec


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
    # Recorded on every persisted decision (schema_proposals.reviewed_by).
    reviewed_by: str = "converge-api"


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
    from ...engines.handlers import _load_domain_convergence_context

    try:
        approval_mode = ApprovalMode(body.approval_mode)
    except ValueError as exc:
        allowed = [m.value for m in ApprovalMode]
        raise HTTPException(
            status_code=422, detail=f"approval_mode must be one of {allowed}"
        ) from exc

    # F-007 / D-10: `domain` selects that tenant's inference rules and
    # enrichment hints — and only those, never the full domain_spec — through
    # the same loader the SDK converge path uses. It is a tenant-scoped read,
    # so it takes the direct-tenant posture (403) and the tenant id grammar
    # (400) every other direct route applies (tenant id == domain spec id).
    domain_hints: dict[str, Any] = {}
    inference_rules: Any = []
    if body.domain:
        require_direct_domain_access()
        try:
            domain_id = validate_tenant_id(body.domain)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        domain_hints, inference_rules = await _load_domain_convergence_context(
            domain_id=domain_id, node_label=None
        )

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
    # convergence_threshold is the uncertainty boundary between targeted and
    # verification passes (U scale 0-10). It is not the improvement delta
    # (default 0.05) and not the field confidence threshold (default 0.85),
    # which it used to set: 2.0 as a confidence delta could never be met.
    convergence_cfg = ConvergenceConfig(
        max_passes=body.max_passes,
        max_budget_tokens=body.max_budget_tokens,
        uncertainty_threshold=body.convergence_threshold,
    )

    try:
        conv_response = await run_convergence_loop(
            request=enrich_request,
            settings=settings,
            kb_resolver=_kb_resolver,
            idem_store=_idem_store,
            inference_rules=inference_rules,
            domain_hints=domain_hints,
            convergence_config=convergence_cfg,
        )
    except Exception as exc:
        state.status = LoopStatus.FAILED
        state.failure_reason = str(exc)
        await store.save(state)
        logger.error("convergence_loop_failed", run_id=state.run_id, error=str(exc), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error during convergence") from exc

    state.accumulated_fields = conv_response.fields or {}
    state.current_pass = conv_response.pass_count
    if conv_response.state != "completed":
        state.status = LoopStatus.FAILED
    elif approval_mode is ApprovalMode.HUMAN:
        # Schema proposals from this run wait for /converge/{run_id}/approve.
        state.status = LoopStatus.HUMAN_HOLD
    else:
        state.status = LoopStatus.CONVERGED
    await store.save(state)

    # F-018: the loop's actual stop reason, not a constant.
    convergence_reason = conv_response.convergence_reason or conv_response.failure_reason or ""
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


def _propose_for_run(run: LoopState, domain_spec: dict[str, Any], domain: str) -> SchemaProposalSet:
    """Schema proposals from one held run's accumulated fields and confidences."""
    return propose_schema(
        batch_results=[
            {
                "final_fields": run.accumulated_fields,
                "final_field_confidences": run.accumulated_confidences,
            }
        ],
        current_yaml=domain_spec,
        domain=domain,
    )


async def _persist_or_503(
    run_id: str,
    domain: str,
    proposal_set: SchemaProposalSet,
    body: ApproveRequestBody,
) -> int:
    """Persist one approval's decisions, or refuse the approval with HTTP 503."""
    try:
        return await persist_approved_revision(
            tenant_id=domain,
            domain=domain,
            batch_run_id=run_id,
            proposal_set=proposal_set,
            decisions=body.decisions,
            reviewed_by=body.reviewed_by,
        )
    except Exception as exc:
        logger.error(
            "schema_revision_persist_failed",
            run_id=run_id,
            domain=domain,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise HTTPException(
            status_code=503,
            detail="Schema revision could not be persisted; approval not applied",
        ) from exc


@router.post("/converge/{run_id}/approve", responses={403: _DIRECT_DOMAIN_ACCESS_RESPONSE})
async def approve_proposals(run_id: str, body: ApproveRequestBody) -> dict[str, Any]:
    require_direct_domain_access()
    store = _get_state_store()
    state = await store.load(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    if state.status != LoopStatus.HUMAN_HOLD:
        raise HTTPException(
            status_code=409, detail=f"Run is {state.status.value}, not awaiting approval"
        )
    if state.domain not in _domain_specs:
        raise HTTPException(status_code=404, detail=f"Domain spec for '{state.domain}' not found")
    # The per-domain lock serialises propose -> apply -> persist -> publish, so
    # a second approval for the same domain proposes against the revision the
    # first one published, not against the snapshot both started from.
    async with _domain_lock(state.domain):
        domain_spec = _domain_specs.get(state.domain, {})
        if not domain_spec:
            raise HTTPException(
                status_code=404, detail=f"Domain spec for '{state.domain}' not found"
            )
        proposal_set = _propose_for_run(state, domain_spec, state.domain)
        applied = applied_field_names(body.decisions, proposal_set)
        updated_yaml = apply_proposals(domain_spec, body.decisions, proposal_set)
        # EIE-IDENT-03: the revision is durable before the registry takes it. An
        # in-memory-only revision vanished on the next restart while every caller
        # had been told it was approved. Identity contract: tenant_id == domain.
        persisted = await _persist_or_503(run_id, state.domain, proposal_set, body)
        # F-247-2: a batch that applies no field is no revision. Startup replays
        # approved rows only, so publishing a bump for it would land the live
        # registry on a version no restart reproduces. Rejections are still
        # persisted above as the audit trail.
        if applied:
            _domain_specs[state.domain] = updated_yaml
        published = _domain_specs[state.domain]
        state.status = LoopStatus.CONVERGED
        await store.save(state)
    applied_set = set(applied)
    return {
        "run_id": run_id,
        "approved": len(applied),
        "rejected": sum(1 for d in body.decisions if not d.approved),
        "unapplied": sorted(
            d.field_name for d in body.decisions if d.approved and d.field_name not in applied_set
        ),
        "persisted": persisted,
        "new_version": published.get("version", "unknown"),
    }


@router.get("/converge/proposals/{domain}", responses={403: _DIRECT_DOMAIN_ACCESS_RESPONSE})
async def get_pending_proposals(domain: str) -> dict[str, Any]:
    require_direct_domain_access()
    store = _get_state_store()
    active_runs = await store.list_active(domain=domain)
    held_runs = [r for r in active_runs if r.status == LoopStatus.HUMAN_HOLD]
    domain_spec = _domain_specs.get(domain, {})
    proposals: list[dict[str, Any]] = []
    for run in held_runs:
        ps = _propose_for_run(run, domain_spec, domain)
        proposals.append(
            {"run_id": run.run_id, "entity_id": run.entity_id, "proposal": ps.model_dump()}
        )
    return {"domain": domain, "pending_count": len(proposals), "proposals": proposals}


@router.post("/scan", responses={403: _DIRECT_DOMAIN_ACCESS_RESPONSE})
async def scan_crm(body: ScanRequestBody) -> DiscoveryReport:
    scan_result, domain_spec = scan_registered_domain(body.domain, body.fields)
    return generate_discovery_report(scan_result, domain_spec)
