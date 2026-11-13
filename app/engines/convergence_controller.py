"""
Convergence Controller — Multi-pass enrichment→inference loop.

Orchestrates the Schema Discovery Loop:
  Each pass: enrich → infer → convergence check → continue or stop.
  Pass 1 stops the loop when the requested fields already clear their bars.
  The improvement-delta check starts on pass 2, once a prior pass exists.

Config: max_passes=3, convergence_threshold=2.0, min_delta=0.05

Uncertainty (D-9 v2, issue #266): U_mean = 10 * (1 - mean c*) is reported as
the diagnostic uncertainty_score; U_action = 10 * (1 - min c*) over the same
required fields drives the planner. The loop converges only when every deciding
field is ready (present value, effective confidence at its own threshold); every
other stop is a bounded termination, never convergence.

Consumed by: chassis handlers.py (action="enrich" with mode="converge")
Produces: Final EnrichResponse with merged fields from all passes + inference results
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import structlog

from ..core.config import Settings
from ..models.loop_schemas import PassContext
from ..models.schemas import (
    DEFAULT_FIELD_CONFIDENCE_THRESHOLD,
    EnrichRequest,
    EnrichResponse,
    resolve_field_thresholds,
)
from ..services.graph_return_channel import EnrichmentTarget, GraphReturnChannel
from ..services.idempotency import IdempotencyStore
from ..services.request_deadline import current_deadline
from .convergence.acquisition_order import latest_withheld
from .convergence.confidence_tracker import (
    UNCERTAINTY_POLICY,
    UNCERTAINTY_SCALE,
    ConfidenceTracker,
    normalize_confidence,
)
from .convergence.convergence_config import ConvergenceConfig  # NEW
from .convergence.cost_tracker import CostTracker
from .convergence.field_evidence import (
    RESPONSE_CONTRACT_V2,
    EvidenceContractError,
    build_field_evidence,
    build_proposals,
    check_response_v2,
    merge_observation_evidence,
)
from .convergence.observation_evidence import observation_evidence
from .field_classifier import DomainClassification, auto_classify_domain
from .inference.rule_loader import build_unlock_index
from .inference_bridge_adapter import InferenceBridge
from .inference_bridge_v2 import prioritize_search_targets
from .meta_prompt_planner import MetaPromptPlanner, PromptPlan
from .search_optimizer import (
    EntitySignals,
    SonarConfig,
    cap_variations,
    resolve_from_classification,
)

logger = structlog.get_logger("convergence_controller")

MAX_PASSES = 3
CONVERGENCE_THRESHOLD = 2.0
MIN_DELTA = 0.05

# ── Module-level classification cache ──────────────────────
_classification_cache: dict[str, DomainClassification] = {}


def _cache_key(domain_spec: dict[str, Any]) -> str:
    """Content digest of the loaded spec.

    A classification is valid only for the exact spec it was computed from: two
    cartridges, or two revisions of one, never share an entry. (The former key,
    declared-domain-name plus node count, was `unknown:1` for every one-node
    native cartridge.)
    """
    canonical = json.dumps(domain_spec, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def get_or_classify_domain(domain_spec: dict[str, Any]) -> DomainClassification:
    """Classify domain fields with module-level caching."""
    key = _cache_key(domain_spec)
    if key in _classification_cache:
        logger.debug("classification_cache_hit", key=key)
        return _classification_cache[key]
    classification = auto_classify_domain(domain_spec)
    _classification_cache[key] = classification
    logger.info(
        "classification_cached",
        domain=classification.domain,
        fields=sum(classification.stats.values()),
        stats=classification.stats,
    )
    return classification


# Confidence-tracker source label for values seeded by a Gate-routed
# graph-inference-result packet (EIE-POST-F001).
GRAPH_INFERENCE_SOURCE = "graph_inference"

# Confidence-tracker source label for values this loop's InferenceBridge derived.
INFERENCE_SOURCE = "inference"

# Convergence stopped because the shared request deadline ran out. Not a
# successful convergence — see _assemble_convergence_response.
DEADLINE_EXHAUSTED_REASON = "deadline_exhausted"
# Stop reason when the planner authorized no provider variation for a pass.
NO_VARIATIONS_AUTHORIZED = "no_variations_authorized"


@dataclass
class PassResult:
    """Result of a single enrichment+inference pass."""

    pass_number: int
    enriched_fields: dict[str, Any] = field(default_factory=dict)
    inferred_fields: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    tokens_used: int = 0
    search_plan: PromptPlan | None = None
    sonar_config: SonarConfig | None = None
    inference_fired: int = 0
    per_field_confidence: dict[str, float] = field(default_factory=dict)  # NEW
    graph_seeded_fields: dict[str, Any] = field(default_factory=dict)
    inference_provenance: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Fields valid replies spoke to and this pass did not admit (response v2).
    withheld_fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The enrichment pass's own outcome (F-003): a failed pass must not be
    # folded into a "completed" converge as though it had merely found nothing.
    state: str = "completed"
    failure_reason: str | None = None
    # Diagnostic U_mean after this pass (WU-02, D-9 v2); None when the pass
    # failed and both scores were carried forward unchanged.
    uncertainty: float | None = None
    # Operational U_action after this pass (D-9 v2). Internal: it drives the
    # next pass's plan and is logged, never serialized into the response.
    action_uncertainty: float | None = None


@dataclass
class ConvergenceState:
    """Accumulated state across all passes."""

    known_fields: dict[str, Any] = field(default_factory=dict)
    confidence_map: dict[str, float] = field(default_factory=dict)
    inferred_fields: dict[str, Any] = field(default_factory=dict)
    pass_results: list[PassResult] = field(default_factory=list)
    total_tokens: int = 0
    # True only when every deciding field met its own threshold. A loop that
    # stopped on budget, deadline, insufficient improvement, max passes or an
    # unsatisfied verification pass terminated without converging; the reason
    # is in convergence_reason.
    converged: bool = False
    convergence_reason: str = ""
    unlock_map: dict[str, float] = field(default_factory=dict)
    # D-9 v2 keeps two scores over one population. Nothing is known before pass
    # 1, which is cold start: both seed at the maximum, so the discovery pass
    # gets the largest variation budget, not the smallest.
    # Diagnostic U_mean: the public uncertainty_score.
    uncertainty_score: float = UNCERTAINTY_SCALE
    # Operational U_action: what the planner receives.
    action_uncertainty: float = UNCERTAINTY_SCALE


async def run_convergence_loop(
    request: EnrichRequest,
    settings: Settings,
    kb_resolver,
    idem_store: IdempotencyStore | None = None,
    enricher=None,
    inference_rules: list[dict] | None = None,
    domain_hints: dict[str, Any] | None = None,
    domain_spec: dict[str, Any] | None = None,
    convergence_config: ConvergenceConfig | None = None,  # NEW
    tenant_id: str | None = None,
    entity_id: str | None = None,
    response_contract: str | None = None,
    subject: Mapping[str, str] | None = None,
    observations: tuple[Mapping[str, Any], ...] = (),
) -> EnrichResponse:
    """
    Multi-pass convergence loop.

    Parameters
    ----------
    request : EnrichRequest
        The original enrichment request from Odoo/Salesforce/CLI.
    settings : Settings
        L9 env-var-driven settings.
    kb_resolver : KBResolver
        Selective KB fragment injector.
    idem_store : IdempotencyStore | None
        Redis idempotency cache.
    enricher : callable | None
        The single-pass enrich_entity function. Injected to avoid circular import.
    inference_rules : list[dict] | None
        YAML-loaded inference rules for this domain.
    domain_hints : dict | None
        enrichment_hints from domain YAML (priority_fields, objective_template, etc.)
    domain_spec : dict | None
        Full domain YAML for field classification and search optimization.
    convergence_config : ConvergenceConfig | None
        Convergence behavior settings (thresholds, pass limits).
    tenant_id, entity_id : str | None
        Identity of the entity being converged. When both are set, each pass
        first consumes this entity's GraphReturnChannel targets — the
        graph-inference-result values CEG sent back through Gate — so graph
        inference feeds the pass it precedes. Without both, no tenant's queue
        is read: a target is never applied across a tenant or entity boundary.
    response_contract : str | None
        None answers in v1. ``eie.converge.response/v2`` adds per-field evidence
        (docs/contracts/converge/response.v2.md). Any other value is refused.
    subject : Mapping[str, str] | None
        The request's resolved ``subject`` (``type``, ``role``), echoed into
        every evidence entry's identity. Never derived here.
    observations : tuple of mappings
        Request v2.1 observations, already validated against the cartridge. They
        are evidence only, folded into a v2 response; they never steer the loop.
    """
    if response_contract not in (None, RESPONSE_CONTRACT_V2):
        raise ValueError(f"unsupported_contract: {response_contract!r}")
    start = time.monotonic()
    state = ConvergenceState()
    bridge = InferenceBridge(
        rules=inference_rules or [],
        domain_spec=domain_spec,
    )

    # ── Convergence config ─────────────────────────────────
    config = convergence_config or ConvergenceConfig()
    # F-017: gaps are fields below the loop's own threshold, not a fixed 0.7.
    # F-019: the domain's priority fields (config first, else the handlers'
    # enrichment_hints) are targeted first.
    # #237: the strict registry of the domain's condition rules and its unlock
    # index inform the planner's ranking; ordering only, never new targets.
    rule_registry = bridge.rule_registry
    required_fields = _required_fields(request)
    planner = MetaPromptPlanner(
        domain_spec=domain_spec,
        rule_registry=rule_registry,
        unlock_index=build_unlock_index(rule_registry) if rule_registry is not None else None,
        confidence_threshold=config.confidence_threshold,
        priority_fields=config.priority_fields or (domain_hints or {}).get("priority_fields"),
        uncertainty_threshold=config.uncertainty_threshold,
    )
    # The tracker owns the one required-field population and per-field bars
    # that readiness, U_mean and U_action are all measured over (D-9 v2).
    confidence_tracker = ConfidenceTracker(
        confidence_threshold=config.confidence_threshold,
        exclude_sources=frozenset({INFERENCE_SOURCE}),
        required_fields=required_fields,
        field_thresholds=request.field_thresholds,
    )

    # ── Domain classification (cached per domain) ──────────
    domain_classification: DomainClassification | None = None
    if domain_spec:
        domain_classification = get_or_classify_domain(domain_spec)
        logger.info(
            "domain_classified",
            domain=domain_classification.domain,
            stats=domain_classification.stats,
            gate_fields=sorted(domain_classification.gate_fields),
        )

    # ── Cost tracker ───────────────────────────────────────
    max_budget = getattr(settings, "max_budget_tokens", 30000)
    if config.max_budget_tokens is not None:
        # A per-run cap can only lower the deployment's budget (F-007).
        max_budget = min(max_budget, config.max_budget_tokens)
    cost_tracker = CostTracker(max_budget_tokens=max_budget)
    # Only the default enricher takes the planner's authorization keyword.
    default_enricher = enricher is None

    # Seed known_fields from the entity's existing data
    state.known_fields = {k: v for k, v in request.entity.items() if v is not None}

    for pass_num in range(1, config.max_passes + 1):
        confidence_tracker.begin_pass(pass_num)
        logger.info(
            "pass_started",
            pass_number=pass_num,
            known_fields=len(state.known_fields),
            inferred_fields=len(state.inferred_fields),
        )

        # ── Deadline check ────────────────────────────────
        # E5 at pass granularity: a shared request deadline stops the loop
        # before it opens a pass it cannot finish and still respond.
        deadline = current_deadline()
        if deadline is not None and deadline.attempt_timeout() is None:
            logger.info(
                "deadline_exhausted",
                pass_number=pass_num,
                remaining_s=round(deadline.remaining(), 3),
            )
            state.convergence_reason = DEADLINE_EXHAUSTED_REASON
            break

        # ── Budget check ──────────────────────────────────
        if not cost_tracker.can_continue():
            logger.info("budget_exhausted", total_tokens=cost_tracker.total_tokens)
            state.convergence_reason = "budget_exhausted"
            break

        # --- GRAPH RETURN (EIE-POST-F001) ---
        # Before planning, so the planner and this pass's request see it.
        graph_seeded: dict[str, Any] = {}
        if tenant_id and entity_id:
            graph_seeded = _apply_graph_targets(
                GraphReturnChannel.get_instance().take_for_entity(tenant_id, entity_id),
                state,
                confidence_tracker,
                pass_num,
            )

        # --- META-PROMPT PLANNING ---
        # D-9 v2: the planner receives the operational U_action, never U_mean.
        pass_context = PassContext(
            pass_number=pass_num,
            budget_remaining=cost_tracker.budget_remaining(),
            previous_uncertainty=state.action_uncertainty,
        )
        search_plan: PromptPlan = planner.plan_pass(
            # The request's null fields stay visible as gaps (F-016); known_fields
            # drops None values on seeding.
            entity_state={**request.entity, **state.known_fields},
            pass_context=pass_context,
            field_confidences=confidence_tracker.get_all_confidences(),
            uncertainty_score=state.action_uncertainty,
            required_fields=required_fields,
            field_thresholds=request.field_thresholds,
            deciding_fields=confidence_tracker.deciding_fields(),
            # Tie-break only: what earlier passes' replies said and withheld.
            withheld_fields=latest_withheld(pr.withheld_fields for pr in state.pass_results),
        )

        # ── Inference-unlock target prioritization ─────────
        # Prefer the unlock_map from the *previous* pass's inference (stored on
        # state after that pass). Empty on Pass 1. Reorders existing priority
        # fields only — never adds or drops targets.
        if state.unlock_map and search_plan.priority_fields:
            field_map = domain_classification.field_map if domain_classification else None
            search_plan.priority_fields = prioritize_search_targets(
                search_plan.priority_fields,
                state.unlock_map,
                field_map,
            )
            logger.info(
                "search_targets_prioritized_by_unlock",
                pass_number=pass_num,
                top_targets=search_plan.priority_fields[:3],
            )

        # ── Variation authority (post-#320 F2) ────────────
        # The planner's pass budget, capped by the caller, is the only count.
        # A zero is a real zero: no provider call, and the loop says why.
        pass_cap = _pass_variation_cap(request, search_plan)
        if pass_cap <= 0:
            logger.info("no_variations_authorized", pass_number=pass_num)
            state.convergence_reason = NO_VARIATIONS_AUTHORIZED
            break

        # ── Resolve optimal Sonar config for this pass ────
        sonar_config: SonarConfig | None = None
        if domain_classification and search_plan:
            total_domain_fields = sum(domain_classification.stats.values())
            null_field_count = max(0, total_domain_fields - len(state.known_fields))
            gate_critical_missing = len(
                domain_classification.gate_fields - set(state.known_fields.keys())
            )
            signals = EntitySignals(
                null_count=null_field_count,
                known_count=len(state.known_fields),
                avg_confidence=(
                    sum(state.confidence_map.values()) / max(len(state.confidence_map), 1)
                ),
                gate_fields_missing=gate_critical_missing,
                has_website=bool(state.known_fields.get("website")),
                pass_number=pass_num,
                allocated_tokens=cost_tracker.budget_remaining(),
            )
            # The planner owns the count (post-#320 F2): the optimizer picks
            # model, context, filters and tokens for the authorized count and
            # the cost estimate is computed for it. cap_variations stays as the
            # mechanical hard cap (#320); with one owner it never changes it.
            sonar_config = resolve_from_classification(
                search_plan_mode=search_plan.mode,
                target_fields=search_plan.priority_fields or [],
                signals=signals,
                classification=domain_classification,
                budget_tokens=cost_tracker.budget_remaining(),
                authorized_variations=pass_cap,
            )
            sonar_config = cap_variations(sonar_config, pass_cap)
            logger.info(
                "sonar_config_resolved",
                pass_number=pass_num,
                model=sonar_config.model.value,
                context_size=sonar_config.search_context_size.value,
                variations=sonar_config.variations,
                estimated_cost=sonar_config.estimated_cost_per_call,
                disable_search=sonar_config.disable_search,
                reason=sonar_config.resolution_reason,
            )

            # Wire cost estimate into tracker
            if sonar_config.estimated_cost_per_call > 0:
                cost_tracker._rate_per_1k = _rate_from_sonar_config(sonar_config)

        # --- BUILD PASS-SPECIFIC REQUEST ---
        pass_request = _build_pass_request(
            request,
            search_plan,
            state,
            pass_num,
            confidence_threshold=config.confidence_threshold,
        )

        # --- SINGLE-PASS ENRICHMENT ---
        # The default enricher receives the planner's authorization so its
        # single-pass heuristic never re-decides the count. An injected
        # enricher keeps its existing call contract.
        enrich_kwargs: dict[str, Any] = {"sonar_config": sonar_config}
        if enricher is None:
            from ..engines.enrichment_orchestrator import enrich_entity

            enricher = enrich_entity
        if default_enricher:
            enrich_kwargs["authorized_variations"] = pass_cap
            if response_contract is not None:
                # Only a v2 answer needs it; a v1 call is exactly what it was.
                enrich_kwargs["report_withheld"] = True

        pass_response: EnrichResponse = await enricher(
            pass_request,
            settings,
            kb_resolver,
            idem_store,
            **enrich_kwargs,
        )
        if search_plan is not None:
            search_plan.variation_count = pass_response.variation_count

        # --- COLLECT ENRICHED FIELDS ---
        # A failed pass is not evidence. Whatever fields its response carries
        # (an injected enricher may return some) are dropped here, before they
        # can reach state, the tracker, inference or the convergence check.
        # Its tokens are still accounted and its failure still recorded.
        provider_fields = pass_response.fields or {}
        if pass_response.state == "failed" and provider_fields:
            logger.warning(
                "failed_pass_fields_discarded",
                pass_number=pass_num,
                fields=sorted(provider_fields),
                failure_reason=pass_response.failure_reason,
            )
            provider_fields = {}
        pass_result = PassResult(
            pass_number=pass_num,
            enriched_fields=provider_fields,
            confidence=pass_response.confidence,
            tokens_used=pass_response.tokens_used,
            search_plan=search_plan,
            sonar_config=sonar_config,
            graph_seeded_fields=graph_seeded,
            state=pass_response.state,
            failure_reason=pass_response.failure_reason,
        )

        # NEW: Extract per-field confidence from consensus engine
        per_field_conf = {}
        if pass_response.feature_vector:
            per_field_conf = pass_response.feature_vector.get("per_field_confidence", {})
        pass_result.per_field_confidence = per_field_conf
        pass_result.withheld_fields = (pass_response.feature_vector or {}).get(
            "withheld_fields"
        ) or {}

        # Merge into accumulated state with per-field confidence
        for field_name, value in pass_result.enriched_fields.items():
            field_conf = normalize_confidence(
                per_field_conf.get(field_name, pass_response.confidence)
            )
            state.known_fields[field_name] = value
            state.confidence_map[field_name] = field_conf
            # Provider evidence for a field this loop had inferred makes it a
            # fact: it is no longer a derivation to re-run or retract.
            state.inferred_fields.pop(field_name, None)

            # NEW: Update confidence tracker
            confidence_tracker.update_field(
                name=field_name,
                value=value,
                confidence=field_conf,
                pass_num=pass_num,
                source="consensus",
            )

        state.total_tokens += pass_result.tokens_used
        cost_tracker.record_pass(pass_num, pass_result.tokens_used)

        # --- INFERENCE PASS ---
        # Rules run over facts only — the record's values, provider evidence and
        # graph-seeded targets — never over this loop's own earlier derivations.
        # A derivation is a function of its inputs: when a later pass moves an
        # input across a rule boundary, re-running the rules replaces it (and
        # re-bounds its confidence by the inputs now in the response); a
        # derivation whose rule no longer fires is retracted rather than carried
        # forward beside the evidence that contradicts it.
        derived_before = _own_derivations(confidence_tracker)
        facts = {k: v for k, v in state.known_fields.items() if k not in derived_before}
        inference_result = bridge.run(
            entity=facts,
            confidence_map={k: v for k, v in state.confidence_map.items() if k in facts},
        )
        pass_result.inferred_fields = inference_result.derived_fields
        pass_result.inference_fired = inference_result.rules_fired
        pass_result.inference_provenance = inference_result.provenance

        for field_name in sorted(derived_before - set(inference_result.derived_fields)):
            _retract_derivation(field_name, state, confidence_tracker, pass_num)

        for field_name, value in inference_result.derived_fields.items():
            state.inferred_fields[field_name] = value
            state.known_fields[field_name] = value
            inferred_conf = normalize_confidence(
                inference_result.confidence_map.get(field_name, 0.7)
            )
            state.confidence_map[field_name] = inferred_conf

            # NEW: Update confidence tracker for inferred fields
            confidence_tracker.update_field(
                name=field_name,
                value=value,
                confidence=inferred_conf,
                pass_num=pass_num,
                source=INFERENCE_SOURCE,
            )

        if hasattr(inference_result, "unlock_map"):
            state.unlock_map = inference_result.unlock_map

        # --- UNCERTAINTY (WU-02 / F-002, D-9 v2) ---
        # Both scores are recomputed over the same required-field population
        # after every successful pass. U_mean is reported; U_action drives the
        # next pass's mode and variation budget. A failed pass learned nothing,
        # so both carry forward.
        if pass_result.state != "failed":
            state.uncertainty_score = confidence_tracker.uncertainty(required_fields)
            state.action_uncertainty = confidence_tracker.action_uncertainty(required_fields)
            pass_result.uncertainty = state.uncertainty_score
            pass_result.action_uncertainty = state.action_uncertainty
        logger.info(
            "uncertainty_scored",
            pass_number=pass_num,
            policy=UNCERTAINTY_POLICY,
            u_mean=round(state.uncertainty_score, 4),
            u_action=round(state.action_uncertainty, 4),
            carried_forward=pass_result.state == "failed",
        )

        state.pass_results.append(pass_result)

        # --- CONVERGENCE CHECK ---
        # A sufficient first pass stops the loop. The improvement-delta check
        # still needs a previous pass to compare, so it stays on pass 2+.
        if confidence_tracker.has_converged():
            state.converged = True
            state.convergence_reason = _convergence_reason(request, config)
            logger.info(
                "converged_confidence_threshold",
                pass_number=pass_num,
                threshold=config.confidence_threshold,
                summary=confidence_tracker.get_pass_summary(),
            )
            break

        if pass_num >= 2 and not confidence_tracker.had_meaningful_improvement(
            config.min_improvement_delta
        ):
            state.convergence_reason = (
                f"insufficient_improvement_delta_{config.min_improvement_delta}"
            )
            logger.info(
                "converged_insufficient_improvement",
                pass_number=pass_num,
                min_delta=config.min_improvement_delta,
            )
            break

        # A verification pass that did not make every field ready ends the loop
        # without converging: one bounded check, not a claim of readiness.
        if search_plan.mode == "verification":
            state.convergence_reason = "verification_pass_complete"
            break
    else:
        # The for-loop ran out of passes without any stop condition firing.
        state.convergence_reason = "max_passes_reached"

    elapsed = int((time.monotonic() - start) * 1000)
    return _assemble_convergence_response(
        state,
        request,
        elapsed,
        domain_classification,
        cost_tracker,
        confidence_tracker,
        inference_context=bridge.describe(),
        response_contract=response_contract,
        subject=subject,
        entity_id=entity_id,
        observations=observations,
    )


def _required_fields(request: EnrichRequest) -> list[str] | None:
    """The fields whose readiness and uncertainty decide: the caller's schema.

    Every requested field except the reserved ``confidence`` key. An omitted or
    empty schema returns None: the loop keeps its schema-free discovery
    behavior and measures the deciding fields it tracks instead.
    """
    names = [name for name in (request.schema_ or {}) if name != "confidence"]
    return names or None


def _convergence_reason(request: EnrichRequest, config: ConvergenceConfig) -> str:
    if request.field_thresholds:
        return "requested_fields_meet_field_thresholds"
    return f"all_fields_above_threshold_{config.confidence_threshold}"


def _own_derivations(confidence_tracker: ConfidenceTracker) -> set[str]:
    """Fields whose current value this loop's InferenceBridge derived.

    The tracker records the source of each field's latest value, so a field the
    bridge derived on an earlier pass and the provider later evidenced reads as
    a fact here, not as a derivation.
    """
    return {
        name
        for name, field_state in confidence_tracker.fields.items()
        if field_state.source_variation == INFERENCE_SOURCE
    }


def _retract_derivation(
    name: str,
    state: ConvergenceState,
    confidence_tracker: ConfidenceTracker,
    pass_num: int,
) -> None:
    """Drop a derivation whose rule did not fire over this pass's facts."""
    state.known_fields.pop(name, None)
    state.inferred_fields.pop(name, None)
    state.confidence_map.pop(name, None)
    confidence_tracker.fields.pop(name, None)
    logger.info("inference_derivation_retracted", pass_number=pass_num, field=name)


def _apply_graph_targets(
    targets: list[EnrichmentTarget],
    state: ConvergenceState,
    confidence_tracker: ConfidenceTracker,
    pass_num: int,
) -> dict[str, Any]:
    """Seed graph-inference targets into convergence state.

    A target fills an unknown field, or replaces a value that this loop
    produced (it has a confidence_map entry) with lower confidence. It never
    replaces a value the request's entity supplied: that is the record's own
    data, which carries no confidence here, so no inference outranks it. The
    channel has already applied CONFIDENCE_FLOOR. Returns the fields this pass
    actually seeded.
    """
    seeded: dict[str, Any] = {}
    for target in targets:
        name = target.field_name
        if name in state.known_fields and (
            name not in state.confidence_map
            or state.confidence_map[name] >= target.source_confidence
        ):
            continue
        state.known_fields[name] = target.seed_value
        state.inferred_fields[name] = target.seed_value
        state.confidence_map[name] = target.source_confidence
        confidence_tracker.update_field(
            name=name,
            value=target.seed_value,
            confidence=target.source_confidence,
            pass_num=pass_num,
            source=GRAPH_INFERENCE_SOURCE,
        )
        seeded[name] = target.seed_value
    if seeded:
        logger.info(
            "graph_targets_applied",
            pass_number=pass_num,
            fields=sorted(seeded),
            received=len(targets),
        )
    return seeded


def _rate_from_sonar_config(config: SonarConfig) -> float:
    """Extract cost rate from sonar config for the cost tracker."""
    from .search_optimizer import CONTEXT_COST_MULTIPLIER, MODEL_COST_PER_1K

    base = MODEL_COST_PER_1K.get(config.model, 0.005)
    mult = CONTEXT_COST_MULTIPLIER.get(config.search_context_size, 1.0)
    return base * mult


def _assemble_convergence_response(
    state: ConvergenceState,
    request: EnrichRequest,
    elapsed: int,
    domain_classification: DomainClassification | None = None,
    cost_tracker: CostTracker | None = None,
    confidence_tracker: ConfidenceTracker | None = None,  # NEW
    inference_context: dict[str, Any] | None = None,
    response_contract: str | None = None,
    subject: Mapping[str, str] | None = None,
    entity_id: str | None = None,
    observations: tuple[Mapping[str, Any], ...] = (),
) -> EnrichResponse:
    """Build the final EnrichResponse from accumulated convergence state."""
    enriched_or_inferred: set[str] = set()
    for pr in state.pass_results:
        enriched_or_inferred.update(pr.enriched_fields.keys())
        enriched_or_inferred.update(pr.inferred_fields.keys())
        enriched_or_inferred.update(pr.graph_seeded_fields.keys())
    final_fields = {k: v for k, v in state.known_fields.items() if k in enriched_or_inferred}
    avg_confidence = sum(state.confidence_map.get(k, 0) for k in final_fields) / max(
        len(final_fields), 1
    )

    # Build per-pass inference metadata
    pass_inferences = []
    for pr in state.pass_results:
        entry: dict[str, Any] = {
            "pass": pr.pass_number,
            "mode": pr.search_plan.mode if pr.search_plan else "unknown",
            "enriched": list(pr.enriched_fields.keys()),
            "inferred": list(pr.inferred_fields.keys()),
            "graph_seeded": list(pr.graph_seeded_fields.keys()),
            "confidence": pr.confidence,
            "rules_fired": pr.inference_fired,
            "inference_provenance": pr.inference_provenance,
            "per_field_confidence": pr.per_field_confidence,  # NEW
            "uncertainty": pr.uncertainty,
        }
        if pr.sonar_config:
            entry["sonar_model"] = pr.sonar_config.model.value
            entry["sonar_context"] = pr.sonar_config.search_context_size.value
            entry["sonar_cost"] = pr.sonar_config.estimated_cost_per_call
        pass_inferences.append(entry)

    # Build feature_vector with classification + cost + confidence metadata
    feature_vector: dict[str, Any] = {}
    if request.schema_:
        # The denominator the fields API needs for real coverage (F-042).
        feature_vector["requested_fields"] = sorted(request.schema_)
    if domain_classification:
        feature_vector["domain_classification"] = domain_classification.to_dict()
    if cost_tracker:
        feature_vector["cost_summary"] = cost_tracker.to_summary(
            total_fields_discovered=len(final_fields)
        ).model_dump()
    if confidence_tracker:  # NEW
        feature_vector["confidence_tracking"] = confidence_tracker.to_dict()
    if inference_context is not None:
        # Which inference context this run executed — or why it executed none.
        # A refused rule set must not read like a run where no rule happened to fire.
        feature_vector["inference"] = inference_context

    # A loop that stopped because the shared request deadline ran out has not
    # converged, however many fields it managed to accumulate first. Reporting
    # "completed" here would be read by Odoo's mapper as status="ok" (it derives
    # its status from `state` alone) and a timed-out, possibly empty result would
    # be injected as a good one. The deadline exists to produce a *bounded
    # failure*, so say so.
    if state.convergence_reason == DEADLINE_EXHAUSTED_REASON:
        response_state = "failed"
        failure_reason: str | None = DEADLINE_EXHAUSTED_REASON
    elif state.convergence_reason == NO_VARIATIONS_AUTHORIZED:
        # No provider call was authorized for a pass: a bounded failure,
        # never "completed" with nothing enriched.
        response_state = "failed"
        failure_reason = NO_VARIATIONS_AUTHORIZED
    elif state.pass_results and all(pr.state == "failed" for pr in state.pass_results):
        # Every enrichment pass failed: the loop stopped on a bounded failure,
        # not on convergence (F-003). Name the last pass's own reason so the
        # consumer sees why, and so no caller persists or writes this back.
        response_state = "failed"
        last_reason = state.pass_results[-1].failure_reason or "unknown"
        failure_reason = f"all_passes_failed: {last_reason}"
    else:
        response_state = "completed"
        failure_reason = None

    response = EnrichResponse(
        fields=final_fields,
        confidence=round(avg_confidence, 4),
        # Variations are provider calls, not tokens: each pass ran the number
        # of variations its search plan budgeted. The previous expression
        # summed tokens_used, which put a token total into a field the
        # consumer reads as a variation count.
        variation_count=sum(
            (pr.search_plan.variation_count if pr.search_plan is not None else 0)
            for pr in state.pass_results
        ),
        uncertainty_score=state.uncertainty_score,  # Actual uncertainty metric
        pass_count=len(state.pass_results),  # Number of passes executed
        inference_version="v3.0.0-convergence",
        processing_time_ms=elapsed,
        tokens_used=state.total_tokens,
        state=response_state,
        failure_reason=failure_reason,
        convergence_reason=state.convergence_reason or None,
        inferences=pass_inferences,
        feature_vector=feature_vector if feature_vector else None,
    )
    if response_contract is None:
        return response
    if confidence_tracker is None:
        raise ValueError("a v2 response needs the loop's confidence tracker")
    return _as_response_v2(
        response, state, request, confidence_tracker, subject, entity_id, observations
    )


def _unknown_reason(state: ConvergenceState) -> str:
    """Why a requested field has no evidence, from how the loop stopped."""
    if not state.pass_results:
        return "not_attempted"
    if all(pr.state == "failed" for pr in state.pass_results):
        return "source_unavailable"
    if state.convergence_reason == "budget_exhausted":
        return "budget_exhausted"
    return "no_evidence"


def _as_response_v2(
    response: EnrichResponse,
    state: ConvergenceState,
    request: EnrichRequest,
    confidence_tracker: ConfidenceTracker,
    subject: Mapping[str, str] | None,
    entity_id: str | None,
    observations: tuple[Mapping[str, Any], ...] = (),
) -> EnrichResponse:
    """Attach response v2 evidence, or fail the response when it cannot be stated."""
    requested = _required_fields(request) or []
    # A failed response admits nothing (response.v2 section 6).
    fields = dict(response.fields) if response.state == "completed" else {}
    provenance: dict[str, dict[str, Any]] = {}
    withheld: dict[str, dict[str, Any]] = {}
    for pr in state.pass_results:
        provenance.update(pr.inference_provenance)
        for name, record in pr.withheld_fields.items():
            withheld[name] = {**record, "pass_number": pr.pass_number}
    produced = {
        name: {
            "value": tracked.current_value,
            "confidence": tracked.confidence,
            "source": tracked.source_variation,
            "pass_number": tracked.pass_number,
        }
        for name, tracked in confidence_tracker.fields.items()
    }
    identity = {
        "subject_ref": entity_id or request.entity.get("id"),
        "subject_type": (subject or {}).get("type"),
        "role": (subject or {}).get("role"),
    }
    try:
        if not identity["subject_ref"]:
            raise EvidenceContractError("the request names no subject identity")
        evidence = build_field_evidence(
            requested=requested,
            fields=fields,
            produced=produced,
            provenance=provenance,
            withheld=withheld,
            entity=request.entity,
            identity=identity,
            unknown_reason=_unknown_reason(state),
        )
        if observations:
            evidence = merge_observation_evidence(
                evidence, observation_evidence(observations, identity)
            )
            # A disagreement turns a produced value into a conflict, which is
            # never admitted, so `fields` is rebuilt from what stays admitted.
            fields = {name: e["value"] for name, e in evidence.items() if e["admitted"]}
        proposals = build_proposals(evidence, identity) if response.state == "completed" else []
        body = {
            "state": response.state,
            "fields": fields,
            "field_evidence": evidence,
            "proposals": proposals,
        }
        check_response_v2(body, requested)
    except EvidenceContractError as exc:
        # Never a partial v2 body, and never a v1 body passed off as v2.
        logger.warning("response_v2_evidence_refused", error=str(exc))
        return response.model_copy(
            update={
                "state": "failed",
                "failure_reason": f"evidence_contract_violation: {exc}",
                "fields": {},
            }
        )
    return response.model_copy(
        update={
            "fields": fields,
            "contract_version": RESPONSE_CONTRACT_V2,
            "field_evidence": evidence,
            "proposals": proposals,
        }
    )


def _build_pass_request(
    original: EnrichRequest,
    plan: PromptPlan,
    state: ConvergenceState,
    pass_num: int = 1,
    *,
    confidence_threshold: float = DEFAULT_FIELD_CONFIDENCE_THRESHOLD,
) -> EnrichRequest:
    """Build a pass-specific EnrichRequest from the search plan.

    A partial consumer threshold map is resolved here against the loop's own
    ``confidence_threshold``, so each pass is filtered by the same per-field
    bars the tracker judges readiness by, not by the standalone default.
    """
    enriched_entity = {**original.entity, **state.known_fields, **state.inferred_fields}

    pass_objective = original.objective
    # A caller-supplied schema is the writeback contract (Odoo asks for
    # `street` and must get `street` back). Discovery priority fields are
    # only the fallback for a request that did not name its targets.
    if original.schema_:
        pass_schema = original.schema_
        # The full schema stays the output contract, so a targeted pass tells
        # the provider which of its fields are still unresolved through the
        # objective, the one prompt input build_prompt() puts in front of it.
        focus = [name for name in plan.priority_fields if name in pass_schema]
        if plan.mode == "targeted" and focus:
            pass_objective = (
                f"{original.objective}\n\n"
                f"PRIORITY THIS PASS: these requested fields are still unresolved; "
                f"research them first: {', '.join(focus)}"
            )
    elif plan.priority_fields:
        pass_schema = dict.fromkeys(plan.priority_fields, "string")
    else:
        pass_schema = None

    return EnrichRequest.model_validate(
        {
            "entity": enriched_entity,
            "object_type": original.object_type,
            "schema": pass_schema,
            "objective": pass_objective,
            "kb_context": original.kb_context,
            "consensus_threshold": original.consensus_threshold,
            "field_thresholds": resolve_field_thresholds(
                pass_schema or {}, original.field_thresholds, confidence_threshold
            ),
            "max_variations": _pass_variation_cap(original, plan),
            "idempotency_key": None,
        }
    )


def _pass_variation_cap(original: EnrichRequest, plan: PromptPlan) -> int:
    """Variations this pass may run: the plan's budget, never above the caller's.

    A plan budget of 0 is a real 0, not a cue to fall back to the caller's cap.
    """
    return max(min(plan.variation_count, original.max_variations), 0)
