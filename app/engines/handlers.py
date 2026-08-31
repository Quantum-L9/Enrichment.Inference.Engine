"""
app/engines/handlers.py
Chassis Handlers — register_handler("enrich", ...) bridge.

Handler signature (L9 contract):
    async def handle_<action>(tenant: str, payload: dict) -> dict

handle_converge serves the ONE canonical converge contract: an EnrichRequest
in, an EnrichResponse out, with no translation. That is what the live Odoo
producer emits (IB-Odoo_19 build_converge_request) and what its mapper reads.
There is no second dialect behind this action: the pre-canonical
`entity_snapshot` adapter was deleted once a consumer inventory found no
active caller.

Integration fixes applied (PR#21/PR#22 merge pass):
    GAP-5: ResultStore.persist_enrich_response called after handle_enrich and handle_converge
    GAP-6: packet_router.notify_graph_sync called after handle_enrich and handle_converge
"""

from __future__ import annotations

from typing import Any

import aiofiles
import structlog

from ..core.config import get_settings
from ..engines.convergence.convergence_config import ConvergenceConfig
from ..engines.convergence_controller import run_convergence_loop
from ..engines.domain_yaml_reader import DomainYamlReader
from ..engines.enrichment_orchestrator import enrich_batch, enrich_entity
from ..engines.schema_discovery import SchemaDiscoveryEngine
from ..models.schemas import (
    COMPLETED_STATE,
    BatchEnrichRequest,
    EnrichRequest,
    EnrichResponse,
)
from ..services.crm.base import CRMType
from ..services.crm.writeback import WriteBackOrchestrator
from ..services.idempotency import IdempotencyStore
from ..services.kb_resolver import KBResolver
from ..services.request_deadline import (
    CANONICAL_CONVERGE_BUDGET_SECONDS,
    Deadline,
    deadline_scope,
)
from ..services.side_effect_coordinator import RequiredSideEffectError
from ..services.simulation_bridge import (
    analyze_leverage,
    brief_to_dict,
    generate_executive_brief,
    simulate,
)

logger = structlog.get_logger("handlers")

_kb: KBResolver | None = None
_idem: IdempotencyStore | None = None
_domain_reader: DomainYamlReader | None = None


def init_handlers(
    kb: KBResolver,
    idem: IdempotencyStore | None = None,
    domain_reader: DomainYamlReader | None = None,
) -> None:
    global _kb, _idem, _domain_reader
    _kb = kb
    _idem = idem
    _domain_reader = domain_reader


async def _persist_and_sync(
    tenant: str,
    payload: dict[str, Any],
    response_dict: dict[str, Any],
    object_type: str,
    *,
    graph_sync: bool = True,
    require_persistence: bool = False,
) -> None:
    """
    Delegate post-enrich side effects to the single SideEffectCoordinator (TASK-021).

    `graph_sync=False` keeps the Gate->GRAPH round trip off a latency-bounded
    caller's path. Persistence still happens synchronously; only the Graph leg
    is excluded.

    `require_persistence=True` makes enrichment persistence a REQUIRED effect:
    a failure to commit it raises `RequiredSideEffectError` instead of being
    logged under a successful-looking answer. Optional effects (graph sync,
    score invalidation, event emission) never raise either way.
    """
    settings = get_settings()
    entity_id = payload.get("entity_id", payload.get("entity", {}).get("id", "unknown"))
    domain = payload.get("domain", settings.default_domain)
    packet_id = None
    if payload.get("packet_id"):
        packet_id = str(payload["packet_id"])
    from app.services.side_effect_coordinator import get_side_effect_coordinator

    await get_side_effect_coordinator().commit_after_enrich(
        tenant=tenant,
        entity_id=entity_id,
        object_type=object_type,
        domain=domain,
        response_dict=response_dict,
        settings=settings,
        packet_id=packet_id,
        idempotency_key=payload.get("idempotency_key"),
        emit_event=True,
        graph_sync=graph_sync,
        require_persistence=require_persistence,
    )


async def handle_enrich(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    request = EnrichRequest.model_validate(payload)
    response = await enrich_entity(request, settings, _kb, _idem)
    result = response.model_dump()

    if response.state == "completed":
        await _persist_and_sync(tenant, payload, result, request.object_type)

    return result


async def handle_enrichbatch(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    batch_req = BatchEnrichRequest.model_validate(payload)
    results = await enrich_batch(batch_req.entities, settings, _kb, _idem)
    succeeded = sum(1 for r in results if r.state == "completed")
    failed = sum(1 for r in results if r.state == "failed")
    return {
        "results": [r.model_dump() for r in results],
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
    }


async def handle_converge(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """SDK handler for action='converge' — the one canonical contract.

    EnrichRequest in, EnrichResponse out, carried untranslated. This is the
    shape the live Odoo producer emits and its mapper reads; there is no
    alternate dialect and no application-specific translation on this path.

    Odoo waits 30 s on the Gate hop. One deadline of
    CANONICAL_CONVERGE_BUDGET_SECONDS governs the *complete* operation —
    validation, domain context, convergence, provider attempts and their retry
    sleeps, required persistence, and response assembly — not just the
    convergence loop. Every provider attempt underneath derives its transport
    timeout from what is left of it, via the deadline installed here.
    """
    import asyncio

    deadline = Deadline.start(CANONICAL_CONVERGE_BUDGET_SECONDS)

    try:
        request = EnrichRequest.model_validate(payload)
    except Exception as exc:
        logger.warning("handlers.converge_parse_failed", tenant=tenant, error=str(exc))
        return _canonical_failure(f"invalid converge request: {exc}")

    entity_id = _canonical_entity_id(payload)

    try:
        with deadline_scope(deadline):
            return await asyncio.wait_for(
                _run_canonical_converge(tenant, payload, request, deadline),
                timeout=max(deadline.remaining(), 0.0),
            )
    except TimeoutError:
        logger.warning(
            "handlers.converge_timeout",
            tenant=tenant,
            entity_id=entity_id,
            timeout_s=CANONICAL_CONVERGE_BUDGET_SECONDS,
        )
        # The response reserve exists so a timing-out request still answers in
        # EnrichResponse semantics. Odoo's mapper derives status from `state`,
        # so a non-completed state routes it to its own degraded handling —
        # no application-specific error envelope is invented here.
        return _canonical_failure(
            f"converge exceeded {CANONICAL_CONVERGE_BUDGET_SECONDS:.0f}s budget for {entity_id}"
        )
    except RequiredSideEffectError as exc:
        # Durable-before-completed. The convergence itself may have succeeded,
        # but a required side effect (enrichment persistence) did not commit,
        # so this operation did NOT complete. Reporting `completed` here would
        # tell Odoo the result is durable when no row exists, and would retire
        # a retry that is still the correct next step.
        logger.error(
            "handlers.converge_required_side_effect_failed",
            tenant=tenant,
            entity_id=entity_id,
            error=str(exc),
        )
        return _canonical_failure(str(exc))
    except Exception as exc:
        logger.exception(
            "handlers.converge_failed",
            tenant=tenant,
            entity_id=entity_id,
            error=str(exc),
        )
        raise


async def _run_canonical_converge(
    tenant: str,
    payload: dict[str, Any],
    request: EnrichRequest,
    deadline: Deadline,
) -> dict[str, Any]:
    """The complete canonical operation, run under the outer deadline."""
    settings = get_settings()

    # `object_type` is where the live Odoo builder carries the domain
    # (`object_type=str(domain)`), so it is the last fallback for the hints
    # lookup. A non-domain object_type simply misses; the loader fails soft.
    domain_id = (
        payload.get("domain_id")
        or payload.get("domain")
        or request.kb_context
        or request.object_type
    )
    domain_hints, inference_rules = await _load_domain_convergence_context(
        domain_id=domain_id,
        node_label=payload.get("node_label"),
    )

    loop_kwargs: dict[str, Any] = {}
    max_passes = payload.get("max_passes")
    if max_passes is not None:
        loop_kwargs["convergence_config"] = ConvergenceConfig(max_passes=int(max_passes))

    response = await run_convergence_loop(
        request=request,
        settings=settings,
        kb_resolver=_kb,
        idem_store=_idem,
        inference_rules=inference_rules,
        domain_hints=domain_hints,
        **loop_kwargs,
    )
    result = response.model_dump()

    if response.state == COMPLETED_STATE:
        persist_payload = {**payload, "domain": domain_id or settings.default_domain}
        # Two invariants meet here.
        #
        # E14 — zero Graph calls on the canonical path. Graph sync awaits up to
        # three Gate attempts at 30 s each with backoff between them, 93 s in
        # the worst case, against a caller that waits 30 s.
        #
        # Durable-before-completed — enrichment persistence IS required for this
        # response to mean anything, so `require_persistence=True` turns a
        # persistence failure into a raised RequiredSideEffectError rather than
        # a logged warning under a "completed" answer. Graph sync, score
        # invalidation and event emission stay optional: their failure is
        # observable but does not falsify the result the caller is holding.
        await _persist_and_sync(
            tenant,
            persist_payload,
            result,
            request.object_type,
            graph_sync=False,
            require_persistence=True,
        )

    logger.info(
        "handlers.converge_ok",
        tenant=tenant,
        entity_id=_canonical_entity_id(payload),
        fields=sorted(result.get("fields", {}).keys()),
        pass_count=result.get("pass_count"),
        state=result.get("state"),
        remaining_s=round(deadline.remaining(), 3),
    )
    return result


def _canonical_entity_id(payload: dict[str, Any]) -> str:
    """Canonical identity: `entity["id"]` — e.g. "res.partner:55" from Odoo.

    `_odoo_entity_id` is read only as a migration-period alias; it carries the
    same value the live producer already puts on `entity["id"]` and never
    defines the contract.
    """
    entity = payload.get("entity")
    if isinstance(entity, dict):
        for key in ("id", "_odoo_entity_id"):
            value = entity.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return "unknown"


def _canonical_failure(reason: str) -> dict[str, Any]:
    """A non-completed EnrichResponse — the canonical way to report failure."""
    return EnrichResponse(state="failed", failure_reason=reason).model_dump()


async def _load_domain_convergence_context(
    *,
    domain_id: str | None,
    node_label: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load optional domain hints + inference rules for converge."""
    domain_hints: dict[str, Any] = {}
    inference_rules: list[dict[str, Any]] = []
    if not domain_id or not _domain_reader:
        return domain_hints, inference_rules

    label = node_label or "Partner"
    try:
        domain_hints = _domain_reader.get_enrichment_hints(domain_id, label)
    except (FileNotFoundError, ValueError, OSError, KeyError) as exc:
        logger.warning(
            "handlers.converge_domain_hints_failed",
            domain_id=domain_id,
            error=str(exc),
        )

    try:
        config = _domain_reader.load(domain_id)
    except (FileNotFoundError, ValueError, OSError) as exc:
        logger.warning(
            "handlers.converge_domain_load_failed",
            domain_id=domain_id,
            error=str(exc),
        )
        return domain_hints, inference_rules

    if config.inference_rules_path:
        from pathlib import Path

        import yaml

        rules_path = Path(config.inference_rules_path)
        if rules_path.exists():
            async with aiofiles.open(rules_path) as f:
                content = await f.read()
                inference_rules = yaml.safe_load(content) or []

    return domain_hints, inference_rules


async def handle_discover(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    request = EnrichRequest.model_validate(payload)
    response = await enrich_entity(request, settings, _kb, _idem)

    current_schema = payload.get("current_schema", {})
    version = payload.get("schema_version", "0.1.0-seed")
    engine = SchemaDiscoveryEngine(current_schema=current_schema, version=version)
    proposal = engine.analyze(
        enriched_fields=response.fields or {},
        inferred_fields={},
        confidence_map=dict.fromkeys(response.fields or {}, response.confidence),
    )
    return {
        "enrichment": response.model_dump(),
        "schema_proposal": {
            "current_version": proposal.current_version,
            "proposed_version": proposal.proposed_version,
            "stage": proposal.stage,
            "new_properties": [
                {
                    "name": p.name,
                    "type": p.field_type,
                    "discovered_by": p.discovered_by,
                    "confidence": p.discovery_confidence,
                }
                for p in proposal.new_properties
            ],
            "proposed_gates": proposal.proposed_gates,
        },
    }


async def handle_simulate(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    crm_field_names: list[str] = payload.get("crm_field_names", [])
    domain_id: str = payload.get("domain_id", "plastics")
    customer_name: str = payload.get("customer_name", tenant)
    query_profile: dict[str, Any] | None = payload.get("query_profile")
    entity_count: int = int(payload.get("entity_count", 20))
    seed: int = int(payload.get("seed", 42))
    use_sonar: bool = bool(payload.get("use_sonar", True))
    company_names: list[str] | None = payload.get("company_names")

    if not crm_field_names:
        raise ValueError("simulate: crm_field_names is required and must be non-empty")

    domain_spec: dict[str, Any] = {}
    if _domain_reader:
        try:
            config = _domain_reader.load(domain_id)
            domain_spec = config.raw_spec if hasattr(config, "raw_spec") else {}
        except (FileNotFoundError, ValueError, OSError) as exc:
            logger.warning("simulate_domain_spec_load_failed", domain_id=domain_id, error=str(exc))

    seed_stats, enriched_stats, _seed_ents, _enr_ents = simulate(
        crm_field_names=crm_field_names,
        domain_spec=domain_spec,
        query_profile=query_profile,
        entity_count=entity_count,
        seed=seed,
        use_sonar=use_sonar,
        sonar_api_key=settings.perplexity_api_key,
        company_names=company_names,
    )

    leverage_points = analyze_leverage(seed_stats, enriched_stats)
    brief = generate_executive_brief(
        customer_name=customer_name,
        domain_id=domain_id,
        seed_stats=seed_stats,
        enriched_stats=enriched_stats,
        leverage_points=leverage_points,
    )
    return brief_to_dict(brief)


async def handle_writeback(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    domain = payload.get("domain", "company")
    canonical = payload.get("canonical", {})
    crm_type_str = payload.get("crm_type", "odoo")
    credentials = payload.get(
        "credentials",
        {
            "url": settings.odoo_url,
            "db": settings.odoo_db,
            "username": settings.odoo_username,
            "password": settings.odoo_password,
        },
    )
    mapping_path = payload.get("mapping_path", "config/crm/odoo_mapping.yaml")

    crm_type = CRMType(crm_type_str)
    orchestrator = WriteBackOrchestrator(
        crm_type=crm_type,
        credentials=credentials,
        mapping_path=mapping_path,
    )
    result = await orchestrator.async_write_back(domain, canonical)
    return {
        "success": result.success,
        "record_id": result.record_id,
        "fields_written": result.fields_written,
        "error": result.error,
    }


def get_handler_map() -> dict[str, Any]:
    return {
        "enrich": handle_enrich,
        "enrichbatch": handle_enrichbatch,
        "converge": handle_converge,
        "discover": handle_discover,
        "simulate": handle_simulate,
        "writeback": handle_writeback,
    }
