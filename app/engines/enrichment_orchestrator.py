"""
Core enrichment orchestrator — the pipeline.

Sequence:
  1. Check idempotency cache
  2. Parse target schema
  3. Resolve KB fragments (supplementary, never a gate)
  4. Compute uncertainty → adaptive variation budget
  5. Build prompt (schema-aware, entity-rich)
  6. Fire N async Sonar variations (bounded by semaphore)
  7. Validate each response (partial acceptance)
  8. Weighted consensus synthesis
  9. Assemble response with full provenance
  10. Cache idempotency key
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from ..core.config import Settings
from ..engines.convergence.field_evidence import withheld_fields
from ..models.schemas import (
    DEFAULT_FIELD_CONFIDENCE_THRESHOLD,
    EnrichRequest,
    EnrichResponse,
)
from ..services.circuit_breaker import CircuitBreaker
from ..services.consensus_engine import (
    apply_field_confidence_thresholds,
    apply_field_score_thresholds,
    synthesize,
)
from ..services.deterministic_provider import PROVIDER_NAME as DETERMINISTIC_PROVIDER
from ..services.deterministic_provider import query_deterministic
from ..services.idempotency import (
    IdempotencyConflictError,
    IdempotencyStore,
    request_fingerprint,
)
from ..services.perplexity_client import SonarResponse, query_perplexity
from ..services.prompt_builder import build_prompt, build_schema_hash
from ..services.request_deadline import current_deadline, provider_attempt_timeout
from ..services.uncertainty_engine import compute_uncertainty
from ..services.validation_engine import ValidationError, validate_response
from ..utils.safe_convert import normalize_confidence

logger = structlog.get_logger("orchestrator")

# failure_reason when the count owner authorized no provider variation.
NO_VARIATIONS_AUTHORIZED = "no_variations_authorized"

# CB_FAILURE_THRESHOLD / CB_COOLDOWN_SECONDS (F-023). Settings() rather than the
# cached get_settings(): building the breaker at import must not freeze the
# process-wide settings cache before tests or the app configure the environment.
breaker = CircuitBreaker.from_settings(Settings())


def _conflict_response(exc: IdempotencyConflictError, start: float) -> EnrichResponse:
    return EnrichResponse(
        state="failed",
        failure_reason=f"idempotency_key_conflict: {exc}",
        processing_time_ms=int((time.monotonic() - start) * 1000),
    )


def _replay_binding(
    request: EnrichRequest,
    idem_store: IdempotencyStore | None,
    idempotency_scope: str | None,
) -> tuple[IdempotencyStore, str, str, str] | None:
    """(store, scope, key, fingerprint) when this request takes part in replay.

    A caller key with no authenticated scope is not cached at all (EIE-FIX-001):
    caching it would put it back in a namespace every caller shares.
    """
    if not request.idempotency_key or idem_store is None:
        return None
    if idempotency_scope is None:
        logger.warning("idempotency_unscoped_bypass")
        return None
    fingerprint = request_fingerprint(request.model_dump(mode="json", exclude={"idempotency_key"}))
    return idem_store, idempotency_scope, request.idempotency_key, fingerprint


async def enrich_entity(
    request: EnrichRequest,
    settings: Settings,
    kb_resolver,
    idem_store: IdempotencyStore | None = None,
    sonar_config=None,
    *,
    idempotency_scope: str | None = None,
    authorized_variations: int | None = None,
    report_withheld: bool = False,
) -> EnrichResponse:
    """Full enrichment pipeline for a single entity.

    `report_withheld` adds `feature_vector["withheld_fields"]`: what valid
    replies said about fields this pass did not admit. The convergence loop
    asks for it only to answer in response v2; without it the response shape
    is exactly what it was.

    Parameters
    ----------
    sonar_config : SonarConfig | None
        When provided by the convergence controller, overrides default model,
        search_context_size, domain_filter, max_tokens, temperature, and
        variation count. This enables right-sized API calls per pass.
    idempotency_scope : str | None
        The authenticated identity that owns `request.idempotency_key`
        (app.services.idempotency.tenant_scope / api_key_scope). Without it
        the key is not replayed or recorded.
    authorized_variations : int | None
        The convergence loop's D-9 v2 pass budget. When given it is the
        target variation count; neither the optimizer's recommendation nor
        the single-pass heuristic re-decides it. Direct enrich omits it and
        keeps ``compute_uncertainty`` as the count owner.
    """
    start = time.monotonic()

    replay = _replay_binding(request, idem_store, idempotency_scope)
    if replay is not None:
        store, scope, key, fingerprint = replay
        try:
            cached = await store.get(scope, key, fingerprint)
        except IdempotencyConflictError as exc:
            return _conflict_response(exc, start)
        if cached:
            return EnrichResponse(**cached)

    # If sonar_config says all fields are inferrable, skip the API call entirely
    if sonar_config and sonar_config.disable_search:
        elapsed = int((time.monotonic() - start) * 1000)
        logger.info(
            "pipeline_skipped_inferrable",
            reason=sonar_config.resolution_reason,
        )
        return EnrichResponse(
            fields={},
            confidence=0.0,
            variation_count=0,
            uncertainty_score=0.0,
            pass_count=1,
            inference_version="v2.2.0",
            processing_time_ms=elapsed,
            tokens_used=0,
            state="completed",
        )

    try:
        target_schema = request.schema_

        kb_data = kb_resolver.resolve(
            kb_context=request.kb_context,
            entity=request.entity,
            max_fragments=3,
        )

        # One target owner per path (post-#320 F2): the convergence planner
        # when it authorized a count, else a resolved SonarConfig, else the
        # single-pass heuristic. A zero from either owner is a real zero, never
        # a cue to fall back to the heuristic. The hard caps are mechanical:
        # request.max_variations (the caller's cap, and in the loop also the
        # pass budget) and the resolved config's count.
        if authorized_variations is not None:
            target = authorized_variations
        elif sonar_config is not None:
            target = sonar_config.variations
        else:
            target = compute_uncertainty(
                entity=request.entity,
                target_schema=target_schema,
                max_variations=request.max_variations,
            )
        variation_count = max(min(target, request.max_variations), 0)
        if sonar_config is not None:
            variation_count = min(variation_count, sonar_config.variations)
        if variation_count == 0:
            elapsed = int((time.monotonic() - start) * 1000)
            logger.info("pipeline_skipped_no_variations_authorized", elapsed_ms=elapsed)
            return EnrichResponse(
                state="failed",
                failure_reason=NO_VARIATIONS_AUTHORIZED,
                variation_count=0,
                consensus_threshold=request.consensus_threshold,
                uncertainty_score=0.0,
                pass_count=1,
                kb_content_hash=kb_data["content_hash"],
                kb_fragment_ids=kb_data["fragment_ids"],
                kb_files_consulted=kb_data["kb_files"],
                tokens_used=0,
                processing_time_ms=elapsed,
            )

        # Determine model — sonar_config overrides settings default
        model = sonar_config.model.value if sonar_config else settings.perplexity_model

        logger.info(
            "pipeline_started",
            entity_name=request.entity.get("Name", request.entity.get("name", "?")),
            object_type=request.object_type,
            variations=variation_count,
            kb_fragments=len(kb_data["fragment_ids"]),
            sonar_optimized=sonar_config is not None,
            model=model,
        )

        payload = build_prompt(
            entity=request.entity,
            object_type=request.object_type,
            objective=request.objective,
            target_schema=target_schema,
            kb_context_text=kb_data["context_text"],
            model=model,
            sonar_config=sonar_config,
        )

        # If sonar_config provides API params, merge them into the payload
        if sonar_config:
            api_params = sonar_config.to_api_params()
            # Overlay sonar_config params onto the payload (model, temperature, max_tokens, etc.)
            payload["model"] = api_params.get("model", payload.get("model"))
            payload["temperature"] = api_params.get("temperature", payload.get("temperature"))
            payload["max_tokens"] = api_params.get("max_tokens", payload.get("max_tokens"))
            if "web_search_options" in api_params:
                payload["web_search_options"] = api_params["web_search_options"]
            if "search_recency_filter" in api_params:
                payload["search_recency_filter"] = api_params["search_recency_filter"]
            if "search_domain_filter" in api_params:
                payload["search_domain_filter"] = api_params["search_domain_filter"]
            if "response_format" in api_params:
                payload["response_format"] = api_params["response_format"]
            if "search_mode" in api_params:
                payload["search_mode"] = api_params["search_mode"]
            if api_params.get("disable_search"):
                payload["disable_search"] = True

        # E4/E5: one attempt's transport timeout comes out of what is left of
        # the shared request deadline, never from a fixed per-attempt constant.
        effective_timeout = provider_attempt_timeout(float(settings.default_timeout_seconds))
        if effective_timeout is None:
            elapsed = int((time.monotonic() - start) * 1000)
            logger.warning(
                "pipeline_skipped_deadline",
                reason="remaining request budget below response reserve",
                elapsed_ms=elapsed,
            )
            return EnrichResponse(
                state="failed",
                failure_reason="request_deadline_exhausted_before_provider_call",
                variation_count=0,
                consensus_threshold=request.consensus_threshold,
                uncertainty_score=0.0,
                pass_count=1,
                kb_content_hash=kb_data["content_hash"],
                kb_fragment_ids=kb_data["fragment_ids"],
                kb_files_consulted=kb_data["kb_files"],
                tokens_used=0,
                processing_time_ms=elapsed,
            )

        logger.info(
            "provider_timeout_resolved",
            effective_timeout_s=round(effective_timeout, 3),
            deadline_bound=current_deadline() is not None,
        )

        sem = asyncio.Semaphore(settings.max_concurrent_variations)

        # EIE-009: the source is a configuration choice, never a fallback. The
        # deterministic source computes the answer from the entity and the target
        # schema, so `enrich` reaches state="completed" — and therefore
        # _persist_and_sync, and therefore the Gate -> CEG leg — with no provider
        # egress. A missing key or an open circuit still fails; nothing selects
        # this implicitly.
        if settings.l9_enrichment_provider == DETERMINISTIC_PROVIDER:

            async def _call() -> SonarResponse:
                return await query_deterministic(
                    entity=request.entity,
                    target_schema=target_schema,
                )

        else:

            async def _call() -> SonarResponse:
                async with sem:
                    return await query_perplexity(
                        payload=payload,
                        api_key=settings.perplexity_api_key,
                        breaker=breaker,
                        timeout=effective_timeout,
                    )

        results = await _collect_variations(
            call=_call,
            variation_count=variation_count,
            target_schema=target_schema,
            threshold=request.consensus_threshold,
            field_thresholds=request.field_thresholds,
            default_field_threshold=DEFAULT_FIELD_CONFIDENCE_THRESHOLD,
        )
        calls_made = len(results)

        valid, raw_payloads, errors, total_tokens = _process_variation_results(
            results, target_schema
        )

        elapsed = int((time.monotonic() - start) * 1000)

        if not valid:
            return EnrichResponse(
                state="failed",
                failure_reason=f"no_valid_responses ({len(errors)} errors: {'; '.join(errors[:3])})",
                variation_count=calls_made,
                consensus_threshold=request.consensus_threshold,
                uncertainty_score=0.0,  # Single-pass mode has no uncertainty tracking
                pass_count=1,
                kb_content_hash=kb_data["content_hash"],
                kb_fragment_ids=kb_data["fragment_ids"],
                kb_files_consulted=kb_data["kb_files"],
                tokens_used=total_tokens,
                processing_time_ms=elapsed,
            )

        # Timeouts are not dissenting votes. Agreement is measured across
        # replies that actually came back, so one good `street` is not
        # discarded because the sibling call never answered.
        # A consumer map may be partial: a requested field it omits is held to
        # the default field confidence threshold, never to consensus_threshold.
        # An empty map on a schema-free request names no field and stays a no-op.
        field_bars = request.field_thresholds is not None and bool(
            target_schema or request.field_thresholds
        )
        # Every valid reply, before any bar removes a value: what a withheld
        # field's evidence is read from (response v2).
        replies = valid
        if field_bars and request.field_thresholds is not None:
            valid = apply_field_confidence_thresholds(
                valid,
                request.field_thresholds,
                requested_fields=target_schema or {},
                default_threshold=DEFAULT_FIELD_CONFIDENCE_THRESHOLD,
            )
        synthesis = synthesize(
            valid,
            request.consensus_threshold,
            total_attempted=len(valid),
        )
        # The bar applies to the score the response carries as well, so dissent
        # or a synthetic median never delivers a field below it (post-#320 F1).
        if field_bars and request.field_thresholds is not None:
            synthesis = apply_field_score_thresholds(
                synthesis,
                request.field_thresholds,
                requested_fields=target_schema or {},
                default_threshold=DEFAULT_FIELD_CONFIDENCE_THRESHOLD,
            )

        withheld: dict[str, Any] | None = (
            withheld_fields(replies, synthesis["fields"], target_schema or {})
            if report_withheld
            else None
        )

        if not synthesis["fields"]:
            return EnrichResponse(
                state="failed",
                failure_reason="no_fields_above_consensus_threshold",
                feature_vector=None if withheld is None else {"withheld_fields": withheld},
                confidence=synthesis["confidence"],
                variation_count=calls_made,
                consensus_threshold=request.consensus_threshold,
                uncertainty_score=0.0,  # Single-pass mode has no uncertainty tracking
                pass_count=1,
                kb_content_hash=kb_data["content_hash"],
                kb_fragment_ids=kb_data["fragment_ids"],
                kb_files_consulted=kb_data["kb_files"],
                enrichment_payload={"raw": raw_payloads},
                tokens_used=total_tokens,
                processing_time_ms=elapsed,
            )

        schema_hash = build_schema_hash(target_schema)

        # NEW: Extract per-field confidence from synthesis
        per_field_confidence = synthesis.get("per_field_confidence", {})

        resp = EnrichResponse(
            fields=synthesis["fields"],
            confidence=round(synthesis["confidence"], 4),
            kb_content_hash=kb_data["content_hash"],
            kb_fragment_ids=kb_data["fragment_ids"],
            kb_files_consulted=kb_data["kb_files"],
            variation_count=calls_made,
            consensus_threshold=request.consensus_threshold,
            uncertainty_score=0.0,  # Single-pass mode has no uncertainty tracking
            pass_count=1,
            inference_version="v2.2.0",
            processing_time_ms=elapsed,
            tokens_used=total_tokens,
            enrichment_payload={"raw": raw_payloads},
            feature_vector={
                "schema_hash": schema_hash,
                "fields_enriched": list(synthesis["fields"].keys()),
                "per_field_confidence": per_field_confidence,  # NEW
                "requested_fields": sorted(request.schema_ or {}),
                **({} if withheld is None else {"withheld_fields": withheld}),
            },
            state="completed",
        )

        if replay is not None:
            store, scope, key, fingerprint = replay
            # A duplicate that was in flight alongside this one may have bound
            # the key first. Answer with what the key holds, never with a result
            # a later retry would contradict.
            try:
                bound = await store.set(scope, key, fingerprint, resp.model_dump())
            except IdempotencyConflictError as exc:
                return _conflict_response(exc, start)
            if bound is not None:
                resp = EnrichResponse(**bound)

        logger.info(
            "pipeline_completed",
            fields_enriched=len(synthesis["fields"]),
            confidence=resp.confidence,
            tokens=total_tokens,
            elapsed_ms=elapsed,
        )

        return resp

    except Exception as exc:
        elapsed = int((time.monotonic() - start) * 1000)
        logger.error("pipeline_fatal", error=str(exc), exc_info=True)
        return EnrichResponse(
            state="failed",
            failure_reason=str(exc),
            processing_time_ms=elapsed,
        )


_RESPONSE_METADATA = frozenset({"confidence", "reasoning", "sources", "citations", "explanation"})


def _has_non_metadata_field(validated: dict[str, Any]) -> bool:
    """True when the normalized reply carries a usable enriched value."""
    for name, value in validated.items():
        if name in _RESPONSE_METADATA:
            continue
        if value in (None, "", [], {}):
            continue
        return True
    return False


def _reply_is_sufficient(
    result: Any,
    target_schema: dict[str, str] | None,
    threshold: float,
    field_thresholds: dict[str, float] | None = None,
    default_field_threshold: float = DEFAULT_FIELD_CONFIDENCE_THRESHOLD,
) -> bool:
    """True when this reply already fills the requested schema at high confidence.

    A later variation must not be sent once this is true. When the consumer
    sent per-field thresholds, each field is judged by its own number, and a
    requested field the map omits by ``default_field_threshold``. It never
    borrows ``threshold``, the agreement bar. A schema-less reply is sufficient
    only when it also contains a non-metadata field.
    """
    if not isinstance(result, SonarResponse):
        return False
    try:
        validated = validate_response(result.data, target_schema)
    except ValidationError:
        return False
    # validate_response already normalized it; a single reply's field score
    # is exactly this assertion (consensus_engine._field_score), so early stop
    # and the downstream score agree.
    confidence = normalize_confidence(validated.get("confidence"))
    if not target_schema:
        if field_thresholds:
            return False
        return confidence >= threshold and _has_non_metadata_field(validated)
    for name in target_schema:
        if name == "confidence":
            continue
        if validated.get(name) in (None, "", [], {}):
            return False
        if field_thresholds is not None:
            if confidence < field_thresholds.get(name, default_field_threshold):
                return False
        elif confidence < threshold:
            return False
    return True


async def _collect_variations(
    *,
    call: Any,
    variation_count: int,
    target_schema: dict[str, str] | None,
    threshold: float,
    field_thresholds: dict[str, float] | None = None,
    default_field_threshold: float = DEFAULT_FIELD_CONFIDENCE_THRESHOLD,
) -> list[Any]:
    """Stop after a sufficient reply. Later variations run together.

    The first call stays sequential so a complete reply does not spend the
    rest of the budget. When that reply is incomplete, every remaining
    variation is gathered at once so the collection is bounded by the slowest
    call rather than the sum of their latencies.
    """
    if variation_count <= 0:
        return []

    async def _one() -> Any:
        try:
            return await call()
        except Exception as exc:
            return exc

    first = await _one()
    results: list[Any] = [first]
    if _reply_is_sufficient(
        first, target_schema, threshold, field_thresholds, default_field_threshold
    ):
        logger.info(
            "variation_short_circuit",
            calls_made=len(results),
            budget=variation_count,
        )
        return results
    remaining = variation_count - 1
    if remaining <= 0:
        return results
    rest = await asyncio.gather(*[_one() for _ in range(remaining)])
    results.extend(rest)
    return results


def _process_variation_results(
    results: list[Any],
    target_schema: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], int]:
    """Partition gather results into valid responses, raw payloads, errors, and token total."""
    valid: list[dict[str, Any]] = []
    raw_payloads: list[dict[str, Any]] = []
    errors: list[str] = []
    total_tokens = 0
    for result in results:
        if isinstance(result, Exception):
            errors.append(f"{type(result).__name__}: {result}")
            logger.warning("variation_failed", error=str(result))
            continue
        if not isinstance(result, SonarResponse):
            continue
        total_tokens += result.tokens_used
        raw_payloads.append(result.data)
        try:
            validated = validate_response(result.data, target_schema)
            valid.append(validated)
        except ValidationError as e:
            errors.append(f"validation: {e}")
    return valid, raw_payloads, errors, total_tokens


async def enrich_batch(
    requests: list[EnrichRequest],
    settings: Settings,
    kb_resolver,
    idem_store: IdempotencyStore | None = None,
    *,
    idempotency_scope: str | None = None,
) -> list[EnrichResponse]:
    """Batch enrichment with bounded concurrency."""
    sem = asyncio.Semaphore(10)

    async def _bounded(req: EnrichRequest) -> EnrichResponse:
        async with sem:
            return await enrich_entity(
                req, settings, kb_resolver, idem_store, idempotency_scope=idempotency_scope
            )

    results = await asyncio.gather(*[_bounded(r) for r in requests])
    return list(results)
