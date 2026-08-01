"""
app/engines/handlers.py
Chassis Handlers — register_handler("enrich", ...) bridge.

Handler signature (L9 contract):
    async def handle_<action>(tenant: str, payload: dict) -> dict

Integration fixes applied (PR#21/PR#22 merge pass):
    GAP-5: ResultStore.persist_enrich_response called after handle_enrich and handle_converge
    GAP-6: packet_router.notify_graph_sync called after handle_enrich and handle_converge
"""

from __future__ import annotations

from typing import Any

import aiofiles
import structlog

from ..core.config import get_settings
from ..engines.convergence_controller import run_convergence_loop
from ..engines.domain_yaml_reader import DomainYamlReader
from ..engines.enrichment_orchestrator import enrich_batch, enrich_entity
from ..engines.schema_discovery import SchemaDiscoveryEngine
from ..models.schemas import BatchEnrichRequest, EnrichRequest
from ..services.crm.base import CRMType
from ..services.crm.writeback import WriteBackOrchestrator
from ..services.idempotency import IdempotencyStore
from ..services.kb_resolver import KBResolver
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
) -> None:
    """
    GAP-5 + GAP-6: Persist enrichment result and dispatch graph sync packet.
    Fire-and-forward — never raises; failures are logged.
    """
    settings = get_settings()
    entity_id = payload.get("entity_id", payload.get("entity", {}).get("id", "unknown"))
    domain = payload.get("domain", settings.default_domain)

    import asyncio

    # GAP-5: Persist to PostgreSQL via ResultStore
    try:
        from ..models.schemas import EnrichResponse
        from ..services.result_store import ResultStore

        store = ResultStore(tenant_id=tenant)
        resp_obj = EnrichResponse.model_validate(response_dict)
        await asyncio.wait_for(
            store.persist_enrich_response(
                response=resp_obj,
                entity_id=entity_id,
                object_type=object_type,
                domain=domain,
                idempotency_key=payload.get("idempotency_key"),
            ),
            timeout=30.0,
        )
        logger.info("handlers.result_persisted", entity_id=entity_id, tenant=tenant, domain=domain)
    except TimeoutError:
        logger.warning("handlers.result_persist_timeout", entity_id=entity_id, tenant=tenant)
    except (ConnectionError, OSError, RuntimeError) as exc:
        logger.warning("handlers.result_persist_failed", entity_id=entity_id, error=str(exc))

    # GAP-6: Graph sync via PacketRouter
    try:
        from ..engines.packet_router import NodeTarget, get_router

        router = get_router(settings)
        await asyncio.wait_for(
            router.notify_graph_sync(
                tenant_id=tenant,
                entity_id=entity_id,
                fields=response_dict.get("fields", {}),
                domain=domain,
            ),
            timeout=15.0,
        )
        router.route_fire_and_forget(
            target=NodeTarget.SCORE,
            action="score-invalidate",
            tenant_id=tenant,
            payload={"entity_id": entity_id, "domain": domain},
        )
    except TimeoutError:
        logger.warning("handlers.graph_sync_timeout", entity_id=entity_id, tenant=tenant)
    except (ConnectionError, OSError) as exc:
        logger.warning("handlers.graph_sync_failed", entity_id=entity_id, error=str(exc))


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
    settings = get_settings()
    request = EnrichRequest.model_validate(payload)

    domain_hints: dict[str, Any] = {}
    inference_rules: list[dict[str, Any]] = []

    domain_id = payload.get("domain_id")
    node_label = payload.get("node_label")
    if domain_id and node_label and _domain_reader:
        domain_hints = _domain_reader.get_enrichment_hints(domain_id, node_label)
        config = _domain_reader.load(domain_id)
        if config.inference_rules_path:
            from pathlib import Path

            import yaml

            rules_path = Path(config.inference_rules_path)
            if rules_path.exists():
                async with aiofiles.open(rules_path) as f:
                    content = await f.read()
                    loaded = yaml.safe_load(content) or []
                    if isinstance(loaded, dict):
                        inference_rules = loaded.get("inference_rules") or loaded.get("rules") or []
                    else:
                        inference_rules = loaded

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
