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
import hashlib
import json
import time
from typing import Any

import structlog

from ..core.config import Settings
from ..models.schemas import EnrichRequest, EnrichResponse
from ..services.circuit_breaker import CircuitBreaker
from ..services.consensus_engine import synthesize
from ..services.idempotency import IdempotencyConflictError, IdempotencyStore
from ..services.perplexity_client import SonarResponse, query_perplexity
from ..services.prompt_builder import build_prompt, build_schema_hash
from ..services.uncertainty_engine import compute_uncertainty
from ..services.validation_engine import ValidationError, validate_response

logger = structlog.get_logger("orchestrator")

breaker = CircuitBreaker(failure_threshold=5, cooldown=60)


async def call_perplexity(
    *,
    payload: dict[str, Any],
    api_key: str,
    breaker: CircuitBreaker,
    timeout: int,
) -> SonarResponse:
    """Test-visible Perplexity entrypoint used by the variation loop."""
    return await query_perplexity(
        payload=payload,
        api_key=api_key,
        breaker=breaker,
        timeout=timeout,
    )


def request_fingerprint(request: EnrichRequest, *, action: str = "enrich") -> str:
    """Hash the semantic request. Transport and idempotency metadata are excluded."""
    body = {
        "action": action,
        "consensus_threshold": request.consensus_threshold,
        "entity": request.entity,
        "kb_context": request.kb_context,
        "max_variations": request.max_variations,
        "object_type": request.object_type,
        "objective": request.objective,
        "target_schema": request.schema_,
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


async def enrich_entity(
    request: EnrichRequest,
    settings: Settings,
    kb_resolver,
    idem_store: IdempotencyStore | None = None,
    sonar_config=None,
    tenant: str | None = None,
    action: str = "enrich",
) -> EnrichResponse:
    """Full enrichment pipeline for a single entity.

    Parameters
    ----------
    sonar_config : SonarConfig | None
        When provided by the convergence controller, overrides default model,
        search_context_size, domain_filter, max_tokens, temperature, and
        variation count. This enables right-sized API calls per pass.
    """
    start = time.monotonic()

    fingerprint = request_fingerprint(request, action=action)
    if request.idempotency_key and idem_store:
        try:
            cached = await idem_store.get(
                request.idempotency_key,
                tenant=tenant,
                fingerprint=fingerprint,
            )
        except IdempotencyConflictError:
            return EnrichResponse(
                state="failed",
                failure_reason="idempotency_conflict",
                processing_time_ms=0,
            )
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

        uncertainty = compute_uncertainty(
            entity=request.entity,
            target_schema=target_schema,
            max_variations=request.max_variations,
        )

        # Use sonar_config.variations if provided, else fall back to uncertainty
        if sonar_config and sonar_config.variations > 0:
            variation_count = sonar_config.variations
        else:
            variation_count = min(uncertainty, request.max_variations)

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

        sem = asyncio.Semaphore(settings.max_concurrent_variations)

        async def _call() -> SonarResponse:
            async with sem:
                return await call_perplexity(
                    payload=payload,
                    api_key=settings.perplexity_api_key,
                    breaker=breaker,
                    timeout=settings.default_timeout_seconds,
                )

        tasks = [_call() for _ in range(variation_count)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        valid, raw_payloads, errors, total_tokens = _process_variation_results(
            results, target_schema
        )

        elapsed = int((time.monotonic() - start) * 1000)

        if not valid:
            return EnrichResponse(
                state="failed",
                failure_reason=f"no_valid_responses ({len(errors)} errors: {'; '.join(errors[:3])})",
                variation_count=variation_count,
                consensus_threshold=request.consensus_threshold,
                uncertainty_score=0.0,  # Single-pass mode has no uncertainty tracking
                pass_count=1,
                kb_content_hash=kb_data["content_hash"],
                kb_fragment_ids=kb_data["fragment_ids"],
                kb_files_consulted=kb_data["kb_files"],
                tokens_used=total_tokens,
                processing_time_ms=elapsed,
            )

        synthesis = synthesize(
            valid,
            request.consensus_threshold,
            total_attempted=variation_count,
        )

        if not synthesis["fields"]:
            return EnrichResponse(
                state="failed",
                failure_reason="no_fields_above_consensus_threshold",
                confidence=synthesis["confidence"],
                variation_count=variation_count,
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
            variation_count=variation_count,
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
            },
            state="completed",
        )

        if request.idempotency_key and idem_store:
            await idem_store.set(
                request.idempotency_key,
                resp.model_dump(),
                tenant=tenant,
                fingerprint=fingerprint,
            )

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
    tenant: str | None = None,
    action: str = "enrichbatch",
) -> list[EnrichResponse]:
    """Batch enrichment with bounded concurrency."""
    sem = asyncio.Semaphore(10)

    async def _bounded(req: EnrichRequest) -> EnrichResponse:
        async with sem:
            return await enrich_entity(
                req, settings, kb_resolver, idem_store, tenant=tenant, action=action
            )

    results = await asyncio.gather(*[_bounded(r) for r in requests])
    return list(results)
