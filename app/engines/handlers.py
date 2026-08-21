"""
app/engines/handlers.py
Chassis Handlers — register_handler("enrich", ...) bridge.

Handler signature (L9 contract):
    async def handle_<action>(tenant: str, payload: dict) -> dict

handle_converge speaks the Odoo plasticos_gate ConvergeRequest wire format
(entity_snapshot + source_urls) and returns map_converge_response-shaped
payloads with allowlisted partner fields for live CRM writeback.

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
from ..models.schemas import BatchEnrichRequest, EnrichRequest
from ..services.crm.base import CRMType
from ..services.crm.writeback import WriteBackOrchestrator
from ..services.idempotency import IdempotencyStore
from ..services.kb_resolver import KBResolver
from ..services.odoo_gate_converge import (
    DEFAULT_CONVERGE_TIMEOUT_SECONDS,
    build_enrich_request,
    build_odoo_converge_error,
    build_odoo_converge_response,
    is_odoo_converge_payload,
    new_run_id,
    parse_odoo_converge_request,
)
from ..services.simulation_bridge import (
    analyze_leverage,
    brief_to_dict,
    generate_executive_brief,
    simulate,
)

logger = structlog.get_logger("handlers")


def _authoritative_tenant(tenant: str, payload: dict[str, Any]) -> str | dict[str, Any]:
    """Transport tenant is authoritative. Payload tenant_id may match, never override."""
    claimed = payload.get("tenant_id")
    if claimed in (None, ""):
        return tenant
    if claimed == tenant:
        return tenant
    return {"status": "rejected", "error": "tenant_mismatch", "tenant_id": tenant}


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
) -> None:
    """
    Delegate post-enrich side effects to the single SideEffectCoordinator (TASK-021).
    Fire-and-forward — never raises; failures are logged inside the coordinator.
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
    )


async def handle_enrich(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    resolved = _authoritative_tenant(tenant, payload)
    if isinstance(resolved, dict):
        return resolved
    tenant = resolved
    settings = get_settings()
    request = EnrichRequest.model_validate(payload)
    response = await enrich_entity(
        request, settings, _kb, _idem, tenant=tenant, action="enrich"
    )
    result = response.model_dump()

    if response.state == "completed":
        await _persist_and_sync(tenant, payload, result, request.object_type)

    return result


async def handle_enrichbatch(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    resolved = _authoritative_tenant(tenant, payload)
    if isinstance(resolved, dict):
        return resolved
    tenant = resolved
    settings = get_settings()
    batch_req = BatchEnrichRequest.model_validate(payload)
    results = await enrich_batch(
        batch_req.entities, settings, _kb, _idem, tenant=tenant, action="enrichbatch"
    )
    succeeded = sum(1 for r in results if r.state == "completed")
    failed = sum(1 for r in results if r.state == "failed")
    return {
        "results": [r.model_dump() for r in results],
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
    }


async def handle_converge(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """SDK handler for action='converge'.

    Primary wire format is Odoo plasticos_gate ConvergeRequest -> map_converge_response.
    Legacy EnrichRequest payloads (entity/object_type/objective) remain supported for
    internal callers.
    """
    resolved = _authoritative_tenant(tenant, payload)
    if isinstance(resolved, dict):
        return resolved
    tenant = resolved
    if is_odoo_converge_payload(payload):
        return await _handle_odoo_converge(tenant, payload)
    return await _handle_legacy_converge(tenant, payload)


async def _handle_odoo_converge(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Odoo Gate converge path — allowlisted partner fields, bounded timeout."""
    import asyncio

    run_id = new_run_id()
    try:
        parsed = parse_odoo_converge_request(payload)
    except ValueError as exc:
        logger.warning("handlers.converge_parse_failed", tenant=tenant, error=str(exc))
        return build_odoo_converge_error(error=str(exc), run_id=run_id)

    settings = get_settings()
    request = build_enrich_request(parsed)
    domain_hints, inference_rules = await _load_domain_convergence_context(
        domain_id=parsed["domain"],
        node_label=payload.get("node_label") or "Partner",
    )
    convergence_config = ConvergenceConfig(max_passes=parsed["max_passes"])

    try:
        response = await asyncio.wait_for(
            run_convergence_loop(
                request=request,
                settings=settings,
                kb_resolver=_kb,
                idem_store=_idem,
                inference_rules=inference_rules,
                domain_hints=domain_hints,
                convergence_config=convergence_config,
            ),
            timeout=DEFAULT_CONVERGE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "handlers.converge_timeout",
            tenant=tenant,
            entity_id=parsed["entity_id"],
            timeout_s=DEFAULT_CONVERGE_TIMEOUT_SECONDS,
            run_id=run_id,
        )
        # Raise so the hub returns an error packet and Odoo falls back to local.
        raise TimeoutError(
            f"converge exceeded {DEFAULT_CONVERGE_TIMEOUT_SECONDS:.0f}s budget "
            f"for {parsed['entity_id']}"
        ) from None
    except Exception as exc:
        logger.exception(
            "handlers.converge_failed",
            tenant=tenant,
            entity_id=parsed["entity_id"],
            run_id=run_id,
            error=str(exc),
        )
        raise

    result = build_odoo_converge_response(
        enrich_response=response,
        parsed=parsed,
        run_id=run_id,
    )

    # Odoo currently treats any non-exception response as success; raise on
    # non-completed convergence so the hub error path triggers local fallback.
    if result.get("status") != "ok":
        raise RuntimeError(result.get("error") or "converge returned non-ok status")

    if result.get("final_fields"):
        persist_payload = {
            **payload,
            "entity_id": parsed["entity_id"],
            "domain": parsed["domain"],
            "entity": request.entity,
            "idempotency_key": request.idempotency_key,
        }
        await _persist_and_sync(
            tenant,
            persist_payload,
            {
                "fields": result["final_fields"],
                "confidence": response.confidence,
                "tokens_used": response.tokens_used,
                "pass_count": response.pass_count,
                "state": response.state,
            },
            request.object_type,
        )

    logger.info(
        "handlers.converge_odoo_ok",
        tenant=tenant,
        entity_id=parsed["entity_id"],
        run_id=run_id,
        fields=sorted(result.get("final_fields", {}).keys()),
        pass_count=result.get("pass_count"),
        status=result.get("status"),
    )
    return result


async def _handle_legacy_converge(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Legacy EnrichRequest-shaped converge (internal / pre-Odoo-gate callers)."""
    settings = get_settings()
    request = EnrichRequest.model_validate(payload)

    domain_id = payload.get("domain_id") or payload.get("domain")
    node_label = payload.get("node_label")
    domain_hints, inference_rules = await _load_domain_convergence_context(
        domain_id=domain_id,
        node_label=node_label,
    )

    response = await run_convergence_loop(
        request=request,
        settings=settings,
        kb_resolver=_kb,
        idem_store=_idem,
        inference_rules=inference_rules,
        domain_hints=domain_hints,
    )
    result = response.model_dump()

    if response.state == "completed":
        await _persist_and_sync(tenant, payload, result, request.object_type)

    return result


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

    try:
        config = _domain_reader.load(domain_id)
    except (FileNotFoundError, ValueError, OSError) as exc:
        logger.warning(
            "handlers.converge_domain_load_failed",
            domain_id=domain_id,
            error=str(exc),
        )
        return domain_hints, inference_rules

    label = _resolve_node_label(config, node_label)
    if label:
        try:
            domain_hints = _domain_reader.get_enrichment_hints(domain_id, label)
        except (FileNotFoundError, ValueError, OSError, KeyError) as exc:
            logger.warning(
                "handlers.converge_domain_hints_failed",
                domain_id=domain_id,
                error=str(exc),
            )

    raw_rules = config.raw_spec.get("inference_rules")
    if isinstance(raw_rules, list):
        inference_rules = raw_rules
    elif config.inference_rules_path:
        from pathlib import Path

        import yaml

        rules_path = Path(config.inference_rules_path)
        if rules_path.exists():
            async with aiofiles.open(rules_path) as f:
                content = await f.read()
                loaded = yaml.safe_load(content) or []
                if isinstance(loaded, list):
                    inference_rules = loaded

    return domain_hints, inference_rules


def _resolve_node_label(config: Any, node_label: str | None) -> str | None:
    if node_label:
        return node_label
    labels = [name for name in config.node_schemas if name]
    if len(labels) == 1:
        return labels[0]
    return None


async def handle_discover(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    resolved = _authoritative_tenant(tenant, payload)
    if isinstance(resolved, dict):
        return resolved
    tenant = resolved
    settings = get_settings()
    request = EnrichRequest.model_validate(payload)
    response = await enrich_entity(request, settings, _kb, _idem, tenant=tenant)

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
    resolved = _authoritative_tenant(tenant, payload)
    if isinstance(resolved, dict):
        return resolved
    tenant = resolved
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
    resolved = _authoritative_tenant(tenant, payload)
    if isinstance(resolved, dict):
        return resolved
    tenant = resolved
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
