"""
app/engines/handlers.py
Chassis Handlers — register_handler("enrich", ...) bridge.

Handler signature (L9 contract):
    async def handle_<action>(tenant: str, payload: dict) -> dict

handle_converge serves the canonical converge contract: an EnrichRequest in,
an EnrichResponse out, with no translation. That is what the live Odoo producer
emits (IB-Odoo_19 build_converge_request) and what its mapper reads. The
pre-canonical `entity_snapshot` / top-level `entity_id` dialect is retired: such
a payload is not a valid EnrichRequest and fails closed as an invalid request.

Integration fixes applied (PR#21/PR#22 merge pass):
    GAP-5: ResultStore.persist_enrich_response called after handle_enrich and handle_converge
    GAP-6: packet_router.notify_graph_sync called after handle_enrich and handle_converge
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import aiofiles
import structlog
import yaml

from ..core.config import get_settings
from ..engines.converge_contract import (
    COMPLETED_STATE,
    degraded_response,
    mark_deadline_degraded,
    restrict_fields_to_schema,
)
from ..engines.converge_request_v2 import (
    ConvergeRequestRefusedError,
    ResolvedConvergeRequest,
    attach_resolution,
    resolve_converge_request_v2,
)
from ..engines.convergence.convergence_config import ConvergenceConfig
from ..engines.convergence.field_evidence import RESPONSE_CONTRACT_V2
from ..engines.convergence_controller import deadline_response_v2, run_convergence_loop
from ..engines.domain_yaml_reader import (
    DomainEnrichmentConfig,
    DomainPublicationError,
    DomainYamlReader,
    default_node_label,
    enrichment_hints_for,
    resolve_cartridge_id,
)
from ..engines.enrichment_orchestrator import enrich_batch, enrich_entity
from ..engines.native_yaml import parse_native_yaml
from ..engines.schema_discovery import SchemaDiscoveryEngine
from ..models.schemas import (
    ENTITY_IDENTITY_ORDER,
    BatchEnrichRequest,
    EnrichRequest,
    EnrichResponse,
    resolve_converge_identity,
    resolve_entity_identity,
)
from ..services.idempotency import IdempotencyStore, tenant_scope
from ..services.kb_resolver import KBResolver
from ..services.request_deadline import (
    CANONICAL_CONVERGE_BUDGET_SECONDS,
    Deadline,
    deadline_scope,
    effective_budget_seconds,
    packet_timeout_ms,
)
from ..services.side_effect_coordinator import PersistenceRequiredError, SideEffectReport
from ..services.simulation_bridge import (
    analyze_leverage,
    brief_to_dict,
    generate_executive_brief,
    simulate,
)

logger = structlog.get_logger("handlers")

# One operation budget for converge: Odoo waits 30 s on the Gate hop.
DEFAULT_CONVERGE_TIMEOUT_SECONDS = CANONICAL_CONVERGE_BUDGET_SECONDS
_COMPLETED_STATE = COMPLETED_STATE

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
    operation_id: str | None = None,
    defer_graph_sync: bool = False,
    graph_fields: Mapping[str, Any] | None = None,
) -> SideEffectReport:
    """
    Delegate post-enrich side effects to the single SideEffectCoordinator (TASK-021).

    `operation_id` is the transport packet id (`packet.header.packet_id`, D-7),
    read by the SDK handler from the immutable, signed header. It is never read
    from the payload: a `payload.packet_id` is caller data and cannot name the
    operation (WU-01 / F-001).

    Fire-and-forward by default — failures are logged inside the coordinator.
    With `require_persistence=True` a persist failure raises
    PersistenceRequiredError instead, so a caller about to answer `completed`
    can refuse to. Nothing is recorded for the key in that case, leaving the
    same logical operation retryable.

    `graph_sync=False` keeps the Gate->GRAPH round trip off a latency-bounded
    caller's path. Persistence still happens synchronously; only the awaited
    Graph leg is excluded. Score invalidation is NOT excluded — it is
    fire-and-forget to SCORE and costs the caller nothing, so a bounded path
    still leaves no stale score behind (EIE-004; see
    app/services/side_effect_coordinator.py for the full reasoning).

    `defer_graph_sync=True` keeps the Graph leg off the caller's path WITHOUT
    dropping it: the key is left pending for its graph leg (with `graph_fields`
    pinned), and the caller starts it detached via the returned report's key
    once its answer is decided.
    """
    settings = get_settings()
    # SEAM-02: identity resolves top-level entity_id, then entity.id, then
    # entity.entity_id. There is no literal fallback — persisting under a
    # placeholder would collapse every unidentified entity of a tenant into one
    # row, so an unidentified payload is refused here instead.
    entity_id = resolve_entity_identity(payload)
    if entity_id is None:
        msg = f"entity identity required: one of {', '.join(ENTITY_IDENTITY_ORDER)}"
        raise ValueError(msg)
    domain = resolve_cartridge_id(
        tenant, requested=payload.get("domain"), configured_default=settings.default_domain
    )
    from app.services.side_effect_coordinator import get_side_effect_coordinator

    return await get_side_effect_coordinator().commit_after_enrich(
        tenant=tenant,
        entity_id=entity_id,
        object_type=object_type,
        domain=domain,
        response_dict=response_dict,
        settings=settings,
        packet_id=operation_id,
        idempotency_key=payload.get("idempotency_key"),
        emit_event=True,
        graph_sync=graph_sync,
        require_persistence=require_persistence,
        node_label=payload.get("node_label") or object_type,
        defer_graph_sync=defer_graph_sync,
        graph_fields=graph_fields,
    )


def admitted_fields(result: Mapping[str, Any]) -> dict[str, Any]:
    """The fields a converge result admits — the only ones that may reach CEG.

    A v2 body (docs/contracts/converge/response.v2.md) names admission per
    field: exactly the `field_evidence` entries with `admitted: true`, with
    their values. A v1 body admits its `fields`, already cut to the request's
    schema (`restrict_fields_to_schema`). Observed, unknown, conflicting or
    otherwise unadmitted entries are never returned.
    """
    evidence = result.get("field_evidence")
    if isinstance(evidence, Mapping):
        return {
            name: entry["value"]
            for name, entry in evidence.items()
            if isinstance(entry, Mapping) and entry.get("admitted") is True and "value" in entry
        }
    return dict(result.get("fields") or {})


def _operation_id(packet: Any) -> str | None:
    """The operation identity: the transport header's packet id (D-7), if any."""
    packet_id = getattr(getattr(packet, "header", None), "packet_id", None)
    return str(packet_id) if packet_id else None


async def handle_enrich(
    tenant: str,
    payload: dict[str, Any],
    packet: Any = None,
) -> dict[str, Any]:
    """SDK handler for action='enrich' (SEAM-02 consumer).

    Declaring a third parameter makes the SDK call it as
    `(tenant, payload, packet)`; the packet is read only for
    `header.packet_id`, the operation identity side effects key on (D-7).

    The producer's EnrichRequest body carries identity at the top level
    (`entity_id`) and in `entity.id`; `entity.entity_id` is the last accepted
    fallback. A body that names none of them is rejected before any provider
    call — identity is what persistence, graph sync and score invalidation key
    on, and `'unknown'` is not an identity.
    """
    if resolve_entity_identity(payload) is None:
        msg = f"enrich: entity identity required: one of {', '.join(ENTITY_IDENTITY_ORDER)}"
        raise ValueError(msg)
    settings = get_settings()
    request = EnrichRequest.model_validate(payload)
    response = await enrich_entity(
        request, settings, _kb, _idem, idempotency_scope=tenant_scope(tenant)
    )
    result = response.model_dump()

    if response.state == "completed":
        await _persist_and_sync(
            tenant, payload, result, request.object_type, operation_id=_operation_id(packet)
        )

    return result


async def handle_enrichbatch(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    batch_req = BatchEnrichRequest.model_validate(payload)
    results = await enrich_batch(
        batch_req.entities, settings, _kb, _idem, idempotency_scope=tenant_scope(tenant)
    )
    succeeded = sum(1 for r in results if r.state == "completed")
    failed = sum(1 for r in results if r.state == "failed")
    return {
        "results": [r.model_dump() for r in results],
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
    }


async def handle_converge(
    tenant: str,
    payload: dict[str, Any],
    packet: Any = None,
) -> dict[str, Any]:
    """SDK handler for action='converge'.

    The canonical contract is EnrichRequest in, EnrichResponse out — the shape
    the live Odoo producer emits and its mapper reads, carried untranslated.
    The pre-canonical `entity_snapshot` / top-level `entity_id` dialect is
    retired; such a payload fails EnrichRequest validation and is answered as
    an invalid request (state="failed"), never translated.

    The third parameter is the SDK's own invocation contract, not a transport
    surface EIE implements: a handler declaring more than two parameters is
    called as `(tenant, payload, packet)`. EIE reads exactly one field off it —
    the operation budget Gate bounded — and never decodes, validates or signs
    it. A direct in-process caller passes nothing and gets the ceiling.
    """
    budget = effective_budget_seconds(
        packet_timeout_ms(packet),
        ceiling_seconds=DEFAULT_CONVERGE_TIMEOUT_SECONDS,
    )
    operation_id = _operation_id(packet)
    return await _handle_canonical_converge(tenant, payload, budget, operation_id)


async def _handle_canonical_converge(
    tenant: str,
    payload: dict[str, Any],
    budget_seconds: float | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Canonical converge — EnrichRequest in, EnrichResponse out, bounded.

    Odoo waits 30 s on the Gate hop. One deadline of
    DEFAULT_CONVERGE_TIMEOUT_SECONDS governs the *complete* operation —
    validation, domain context, convergence, provider attempts and their retry
    sleeps, persistence, and response assembly — not just the convergence loop.
    Every provider attempt underneath derives its transport timeout from what
    is left of it, via the deadline installed here.
    """
    import asyncio

    # Resolved here, not as a default argument: a default binds at import time,
    # which would freeze the module constant and silently ignore any later
    # reassignment of it.
    if budget_seconds is None:
        budget_seconds = DEFAULT_CONVERGE_TIMEOUT_SECONDS
    deadline = Deadline.start(budget_seconds)

    # Request v2 (docs/contracts/converge/request.v2.md) resolves completely
    # against the cartridge it names, or is refused whole: nothing runs, and
    # nothing falls back to v1 resolution. A v1 body resolves to None here and
    # continues exactly as before.
    cartridge: DomainEnrichmentConfig | None = None
    try:
        resolved = resolve_converge_request_v2(tenant, payload, _domain_reader)
    except ConvergeRequestRefusedError as exc:
        logger.warning("handlers.converge_v2_refused", tenant=tenant, code=exc.code)
        return _canonical_failure(str(exc))
    if resolved is not None:
        payload, cartridge = resolved.body, resolved.config

    try:
        request = EnrichRequest.model_validate(payload)
    except Exception as exc:
        logger.warning("handlers.converge_parse_failed", tenant=tenant, error=str(exc))
        return _canonical_failure(f"invalid converge request: {exc}")

    # Identity is resolved exactly once, here, and handed down: it is what the
    # convergence loop gives GraphReturnChannel and what persistence keys on, so
    # nothing downstream re-derives it from `entity`. There is no placeholder id
    # — `'unknown'` would collapse every unidentified entity of a tenant into
    # one row; a body that names no identity is refused by `_persist_and_sync`.
    entity_id = resolve_converge_identity(payload)

    # Work that runs only once the answer is decided `completed`, outside the
    # deadline (the detached graph leg). A timeout or failure discards it.
    after_completed: list[Callable[[], None]] = []
    try:
        with deadline_scope(deadline):
            result = await asyncio.wait_for(
                _run_canonical_converge(
                    tenant,
                    payload,
                    request,
                    deadline,
                    entity_id,
                    operation_id,
                    cartridge,
                    resolved,
                    after_completed=after_completed,
                ),
                timeout=max(deadline.remaining(), 0.0),
            )
    except TimeoutError:
        logger.warning(
            "handlers.converge_timeout",
            tenant=tenant,
            entity_id=entity_id,
            timeout_s=budget_seconds,
        )
        # The response reserve exists so a timing-out request still answers in
        # EnrichResponse semantics. Odoo's mapper derives status from `state`,
        # so a non-completed state routes it to its own degraded handling —
        # no Odoo-specific error envelope is invented here. Contract v1 names
        # the state: `degraded`, with no partial result attached.
        reason = f"converge exceeded {budget_seconds:.0f}s budget for {entity_id}"
        if resolved is None:
            return degraded_response(reason)
        # A v2 request is answered in v2 even here (response.v2 section 6).
        body = deadline_response_v2(
            request, resolved.subject, entity_id, resolved.observations, reason
        ).model_dump()
        attach_resolution(body, resolved)
        return body
    except Exception as exc:
        logger.exception(
            "handlers.converge_failed",
            tenant=tenant,
            entity_id=entity_id,
            error=str(exc),
        )
        raise
    if result.get("state") == _COMPLETED_STATE:
        for action in after_completed:
            action()
    return result


async def _run_canonical_converge(
    tenant: str,
    payload: dict[str, Any],
    request: EnrichRequest,
    deadline: Deadline,
    entity_id: str | None,
    operation_id: str | None = None,
    cartridge: DomainEnrichmentConfig | None = None,
    resolved: ResolvedConvergeRequest | None = None,
    *,
    after_completed: list[Callable[[], None]] | None = None,
) -> dict[str, Any]:
    """The complete canonical operation, run under the outer deadline.

    `after_completed` collects what the caller runs once the answer is decided
    `completed`, outside the deadline: here, the detached graph leg.

    `cartridge` is the revision a v2 request was resolved and pinned against;
    hints, rules and spec are then taken from it, never from a second read.

    `entity_id` is the identity the caller resolved once from the body
    (`resolve_converge_identity`; None when the body names none); it is not
    re-derived here. The loop, the GraphReturnChannel lookups it performs, and
    persistence all key on it.
    """
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
    domain_hints, inference_rules, domain_spec = await _load_domain_converge_bundle(
        domain_id=domain_id,
        node_label=payload.get("node_label"),
        config=cartridge,
    )

    loop_kwargs: dict[str, Any] = {}
    max_passes = payload.get("max_passes")
    if max_passes is not None:
        loop_kwargs["convergence_config"] = ConvergenceConfig(max_passes=int(max_passes))
    if resolved is not None:
        # A v2 request is answered in v2: per-field evidence for its subject,
        # with its observations folded in as evidence only.
        loop_kwargs.update(
            response_contract=RESPONSE_CONTRACT_V2,
            subject=resolved.subject,
            observations=resolved.observations,
        )

    # EIE-POST-F001: identity lets each pass consume this entity's
    # graph-inference-result targets from GraphReturnChannel.
    response = await run_convergence_loop(
        request=request,
        settings=settings,
        kb_resolver=_kb,
        idem_store=_idem,
        inference_rules=inference_rules,
        domain_hints=domain_hints,
        # The effective revision's full spec, from the same single read as the
        # hints and rules, so field classification and planning see the domain.
        domain_spec=domain_spec,
        tenant_id=tenant,
        entity_id=entity_id,
        **loop_kwargs,
    )
    result = response.model_dump()
    # Contract v1, applied before persistence so the durable record and the
    # answer are the same object: fields ⊆ request.schema, and a deadline stop
    # is `degraded`, never `completed`.
    dropped = restrict_fields_to_schema(result, request.schema_)
    if dropped:
        logger.info("handlers.converge_fields_outside_schema_dropped", dropped=dropped)
    mark_deadline_degraded(result)
    if resolved is not None:
        attach_resolution(result, resolved)

    if response.state == _COMPLETED_STATE:
        # The top-level `entity_id` is the first key of the SEAM-02 order that
        # `_persist_and_sync` applies, so writing the once-resolved id there is
        # what makes persistence key on the same id the loop used, rather than
        # re-deriving one from `entity` (which may carry only the Odoo alias).
        persist_payload = {
            **payload,
            "entity_id": entity_id,
            "domain": resolve_cartridge_id(
                tenant, requested=domain_id, configured_default=settings.default_domain
            ),
        }
        # E14: zero AWAITED Graph calls on the canonical path. Graph sync
        # awaits up to two Gate attempts at 10 s each with a 1 s backoff
        # between them (packet_router._MAX_ATTEMPTS / _ROUTE_TIMEOUT) — 21 s in
        # the worst case, most of a caller's 30 s budget. Persistence stays
        # synchronous because the response depends on it; Graph does not.
        #
        # Converge results still reach CEG (converge_graph_sync_enabled): the
        # graph leg is deferred — the key is left pending for it with only the
        # admitted fields pinned — and started detached by the caller after the
        # answer is decided `completed`, outside the deadline. Its failure
        # leaves the key pending for the graph leg; the answer never changes.
        #
        # require_persistence=True is what makes `completed` mean durable. The
        # producer treats a completed EnrichResponse as the authoritative record
        # of this operation and does not ask again, so answering `completed` on
        # a result no store holds loses the work silently — the enrichment is
        # gone, and the once-per-key guard suppresses the retry that would have
        # recovered it.
        defer_graph = settings.converge_graph_sync_enabled
        try:
            report = await _persist_and_sync(
                tenant,
                persist_payload,
                result,
                request.object_type,
                graph_sync=False,
                require_persistence=True,
                operation_id=operation_id,
                defer_graph_sync=defer_graph,
                graph_fields=admitted_fields(result) if defer_graph else None,
            )
        except PersistenceRequiredError as exc:
            logger.error(
                "handlers.converge_persistence_failed",
                tenant=tenant,
                entity_id=entity_id,
                error=str(exc),
            )
            # Reported in EnrichResponse semantics: the producer's mapper reads
            # state != "completed" and routes the run to its own degraded
            # handling, leaving the logical operation retryable.
            return _canonical_failure(f"result not durable: {exc}")
        if report.graph_deferred and after_completed is not None:
            after_completed.append(_graph_leg_starter(report.key, settings))

    logger.info(
        "handlers.converge_ok",
        tenant=tenant,
        entity_id=entity_id,
        fields=sorted(result.get("fields", {}).keys()),
        pass_count=result.get("pass_count"),
        state=result.get("state"),
        remaining_s=round(deadline.remaining(), 3),
    )
    return result


def _graph_leg_starter(key: str, settings: Any) -> Callable[[], None]:
    """Start the deferred graph leg of `key`, detached; see `schedule_graph_leg`."""

    def start() -> None:
        from app.services.side_effect_coordinator import get_side_effect_coordinator

        get_side_effect_coordinator().schedule_graph_leg(key, settings=settings)

    return start


def _canonical_failure(reason: str) -> dict[str, Any]:
    """A non-completed EnrichResponse — the canonical way to report failure."""
    return EnrichResponse(state="failed", failure_reason=reason).model_dump()


async def _load_domain_convergence_context(
    *,
    domain_id: str | None,
    node_label: str | None,
) -> tuple[dict[str, Any], Any]:
    """Domain hints + inference rules; see `_load_domain_converge_bundle`."""
    hints, rules, _spec = await _load_domain_converge_bundle(
        domain_id=domain_id, node_label=node_label
    )
    return hints, rules


async def _load_domain_converge_bundle(
    *,
    domain_id: str | None,
    node_label: str | None,
    config: DomainEnrichmentConfig | None = None,
) -> tuple[dict[str, Any], Any, dict[str, Any] | None]:
    """Load optional domain hints + inference rules + the full spec for converge.

    The third element is the effective revision's raw spec, or None when no
    domain resolved; all three come from one `DomainYamlReader.load`.

    An unknown domain converges without inference. A loaded domain's rules come
    from `inference_rules_path`, else the spec's own `inference_rules`; they are
    returned unvalidated — InferenceBridge accepts the set whole or reports it refused.

    The enrichment hints are the named node's. With no `node_label`, they are
    the spec's only node's; a multi-node spec with no named node yields no hints
    (logged), never a hard-coded label the spec may not define (#238).

    A relative `inference_rules_path` resolves against the directory of the
    spec file that declared it, so a spec loads the same rules whatever the
    process working directory is (#242); an absolute path is unambiguous. A
    referenced file that is missing or unreadable is returned as its error text:
    InferenceBridge refuses any non-list rule set, so the response reports
    `invalid_rules`, and the warning logged here names the path and the cause. A
    declared rule set that failed to load must not read like a domain that
    declared none.
    """
    domain_hints: dict[str, Any] = {}
    inference_rules: Any = []
    if not domain_id or not _domain_reader:
        return domain_hints, inference_rules, None

    # Resolve once: pointer publication between two reads must never pair old
    # enrichment hints with a different revision's rules.
    # A v2 request hands in the revision it was pinned against: no second read.
    try:
        config = config if config is not None else _domain_reader.load(domain_id)
    except DomainPublicationError:
        raise
    except (FileNotFoundError, ValueError, OSError, yaml.YAMLError) as exc:
        if _domain_reader.require_published:
            raise
        logger.warning(
            "handlers.converge_domain_load_failed",
            domain_id=domain_id,
            error=str(exc),
        )
        return domain_hints, inference_rules, None
    domain_hints = _node_hints(config, domain_id, node_label)
    logger.info(
        "handlers.converge_effective_revision",
        domain_id=domain_id,
        version=config.version,
        source_sha256=config.source_sha256,
        approval_ref=config.approval_ref,
    )

    if config.inference_rules_path:
        inference_rules = await _load_referenced_rules(config, domain_id)
    else:
        inference_rules = config.raw_spec.get("inference_rules") or []

    return domain_hints, inference_rules, config.raw_spec or None


def _node_hints(
    config: DomainEnrichmentConfig, domain_id: str, node_label: str | None
) -> dict[str, Any]:
    """The named node's hints; with no name, the spec's only node's (#238)."""
    label = node_label or default_node_label(config)
    if label is None:
        logger.warning(
            "handlers.converge_node_label_ambiguous",
            domain_id=domain_id,
            nodes=sorted(config.node_schemas),
        )
        return {}
    return enrichment_hints_for(config, label)


async def _load_referenced_rules(config: DomainEnrichmentConfig, domain_id: str) -> Any:
    """Read a spec's `inference_rules_path`, resolved against the spec's directory (#242).

    A file that cannot be read or parsed is returned as its error text, so
    InferenceBridge reports `invalid_rules` naming the path.
    """
    from pathlib import Path

    rules_path = Path(str(config.inference_rules_path))
    if not rules_path.is_absolute() and config.source_dir:
        rules_path = Path(config.source_dir) / rules_path
    try:
        async with aiofiles.open(rules_path) as f:
            content = await f.read()
        return _rules_section(parse_native_yaml(content))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "handlers.converge_inference_rules_unreadable",
            domain_id=domain_id,
            path=str(rules_path.resolve()),
            error=str(exc),
        )
        return f"inference_rules_path {rules_path.resolve()} could not be loaded: {exc}"


def _rules_section(document: Any) -> Any:
    """A referenced rules file is a YAML document with an `inference_rules` section.

    That is `rule_loader.load_rules`'s contract. A bare list is also accepted.
    Anything else is passed through so InferenceBridge reports it refused.
    """
    if isinstance(document, dict) and "inference_rules" in document:
        return document["inference_rules"]
    return document or []


async def handle_discover(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    request = EnrichRequest.model_validate(payload)
    response = await enrich_entity(
        request, settings, _kb, _idem, idempotency_scope=tenant_scope(tenant)
    )

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
    """Demo-only driver for the simulation fixture (PREVIEW-01).

    `simulate` is not a runtime action: it is absent from
    app.main.RUNTIME_ALLOWED_ACTIONS and from get_handler_map(), so the SDK
    runtime refuses a `simulate` packet before any handler runs, and Gate holds
    no route for it. This function remains importable for in-process demo and
    test drivers only. Its output is synthetic and is labelled as such by
    simulation_bridge.brief_to_dict.

    EIE-SEC-01: `customer_name` is a display label the caller supplies
    explicitly; it never defaults to the tenant id.
    """
    settings = get_settings()
    crm_field_names: list[str] = payload.get("crm_field_names", [])
    domain_id = payload.get("domain_id")
    customer_name = payload.get("customer_name")
    query_profile: dict[str, Any] | None = payload.get("query_profile")
    entity_count: int = int(payload.get("entity_count", 20))
    seed: int = int(payload.get("seed", 42))
    use_sonar: bool = bool(payload.get("use_sonar", True))
    company_names: list[str] | None = payload.get("company_names")

    if not crm_field_names:
        raise ValueError("simulate: crm_field_names is required and must be non-empty")
    if not isinstance(customer_name, str) or not customer_name.strip():
        raise ValueError("simulate: customer_name is required; it does not default to the tenant")
    if not isinstance(domain_id, str) or not domain_id:
        raise ValueError("simulate: domain_id is required; no cartridge is assumed")

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


async def handle_writeback(
    tenant: str,
    payload: dict[str, Any],
    packet: Any | None = None,
) -> dict[str, Any]:
    """Refuse CRM mutation until the protected trusted-context seam is installed.

    U00 established that this revision has no authoritative tenant-to-connector
    binding, record/field-bound human approval source, or durable CRM operation
    journal. The SDK supplies ``tenant`` and may supply ``packet``, but neither
    may be treated as a source for any missing approval or credential context.
    In particular, this handler deliberately ignores caller-supplied CRM type,
    credentials, mapping path, tenant, policy flags, and approval references.
    """
    del tenant, payload, packet
    logger.warning("writeback_refused_trusted_context_unavailable")
    return {
        "success": False,
        "status": "disabled",
        "record_id": "",
        "attempted_fields": [],
        "fields_written": [],
        "skipped_fields": [],
        "error": "writeback_disabled_pending_trusted_context_and_durable_journal",
        "provider_ack": None,
        "reconciliation_state": None,
    }


def get_handler_map() -> dict[str, Any]:
    # PREVIEW-01: `simulate` is deliberately absent — it is a demo fixture, not
    # a runtime action (see handle_simulate and app.main.RUNTIME_ALLOWED_ACTIONS).
    return {
        "enrich": handle_enrich,
        "enrichbatch": handle_enrichbatch,
        "converge": handle_converge,
        "discover": handle_discover,
        "writeback": handle_writeback,
    }
