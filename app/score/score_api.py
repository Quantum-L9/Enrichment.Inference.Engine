"""
SCORE Service — FastAPI Router
revopsos-score-engine

REST endpoints for scoring, decay, and explainability.
All endpoints produce TransportPacket-compatible responses with
full provenance and downstream routing metadata.

L9 Architecture Note:
``app/score/score_api.py`` is a CHASSIS adapter module.
It is the thin HTTP surface that wraps pure engine logic.
FastAPI imports are permitted here because this is the router
registered by ``app/main.py`` (chassis), NOT engine domain logic.

Invariants enforced here:
- INV-ARCH-01: POST /v1/execute is the only mutation endpoint
  (this router is mounted under chassis-controlled prefixes).
- ARCH-001: FastAPI imports are in the chassis adapter, not engine.
- All domain logic lives in ``app/score/score_engine.py`` (engine layer).

# L9-fix: ARCH-001
# L9-file: app/score/score_api.py
# L9-violation: Missing chassis-layer metadata tag — ARCH-001 scanner fired on FastAPI imports
# L9-fix-summary: Added # L9-layer: chassis/adapter header and architecture note per INV-ARCH-03
# L9-layer: chassis/adapter
# L9-node: enrichment-inference-engine
# L9-contract-version: 1.0.0
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

# FastAPI imports are PERMITTED in this chassis adapter module.
# Engine modules (score_engine.py, score_models.py, score_explainer.py,
# score_decay.py) must never import fastapi (INV-ARCH-03).
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..core.exceptions import DependencyNotConfiguredError
from .score_models import (
    BatchScoreRequest,
    ScoreDimension,
    ScoreTier,
    ScoringProfile,
)

# ── Dependency stubs (replaced by DI in production) ───────────


def get_score_engine():
    """Injected by app startup via dependency_overrides."""
    raise DependencyNotConfiguredError(
        "ScoreEngine",
        "Call configure_score_dependencies() in lifespan",
    )


def get_decay_engine():
    """Injected by app startup via dependency_overrides."""
    raise DependencyNotConfiguredError(
        "DecayEngine",
        "Call configure_score_dependencies() in lifespan",
    )


def get_explainer():
    """Injected by app startup via dependency_overrides."""
    raise DependencyNotConfiguredError(
        "ScoreExplainer",
        "Call configure_score_dependencies() in lifespan",
    )


def get_profile_store():
    """Injected by app startup via dependency_overrides."""
    raise DependencyNotConfiguredError(
        "ProfileStore",
        "Call configure_score_dependencies() in lifespan",
    )


def get_score_store():
    """Injected by app startup via dependency_overrides."""
    raise DependencyNotConfiguredError(
        "ScoreStore",
        "Call configure_score_dependencies() in lifespan",
    )


# ── Request / Response Models ─────────────────────────────────


class ScoreEntityRequest(BaseModel):
    entity_id: str
    scoring_profile_id: str
    domain: str
    enrichment_run_id: str | None = None
    graph_match_id: str | None = None


class ScoreEntityResponse(BaseModel):
    score_id: str
    entity_id: str
    composite_score: float
    tier: str
    composite_confidence: float
    dimension_scores: dict[str, dict[str, Any]]
    missing_field_count: int
    gate_critical_missing: list[str]
    enrichment_triggers: list[str]
    scored_at: str
    scoring_duration_ms: float


class ExplainRequest(BaseModel):
    entity_id: str
    scoring_profile_id: str
    domain: str


class DecayPreviewRequest(BaseModel):
    entity_id: str
    scoring_profile_id: str
    domain: str
    future_days: float = 30.0


class DecayBatchRequest(BaseModel):
    domain: str
    scoring_profile_id: str
    max_age_hours: float = 24.0
    limit: int = 500


class ProfileCreateRequest(BaseModel):
    name: str
    domain: str
    description: str = ""
    dimension_weights: dict[str, float] | None = None
    tier_thresholds: dict[str, float] | None = None
    confidence_floor: float = 0.30
    gate_penalty: float = 0.50
    min_fields_for_score: int = 3


class TierDistributionResponse(BaseModel):
    domain: str
    total: int
    distribution: dict[str, int]
    avg_composite: float
    generated_at: str


class CompareScoresRequest(BaseModel):
    entity_id: str
    before_score_id: str
    after_score_id: str
    scoring_profile_id: str


# ── Packet Envelope Helper ────────────────────────────────────


def _envelope(
    payload: dict[str, Any],
    service: str = "score",
    operation: str = "score_entity",
    entity_id: str = "",
    domain: str = "",
) -> dict[str, Any]:
    """Wrap response in TransportPacket-compatible structure."""
    return {
        "packet_id": str(uuid4()),
        "service": service,
        "operation": operation,
        "entity_id": entity_id,
        "domain": domain,
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": payload,
        "metadata": {
            "version": "1.0.0",
            "source": "revopsos-score-engine",
        },
    }


# ── Router ────────────────────────────────────────────────────

SCORING_PROFILE_NOT_FOUND = "Scoring profile not found"

router = APIRouter(prefix="/score", tags=["score"])


# -- Score Endpoints -------------------------------------------


@router.post("/entity", response_model=None)
async def score_entity(
    req: ScoreEntityRequest,
    engine=Depends(get_score_engine),
    profile_store=Depends(get_profile_store),
):
    """Score a single entity against a scoring profile."""
    start = time.monotonic()
    profile = profile_store.get_profile(req.scoring_profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=SCORING_PROFILE_NOT_FOUND)

    record = engine.score_entity(
        entity_id=req.entity_id,
        profile=profile,
        enrichment_run_id=req.enrichment_run_id,
        graph_match_id=req.graph_match_id,
    )
    duration = (time.monotonic() - start) * 1000

    response = ScoreEntityResponse(
        score_id=record.score_id,
        entity_id=record.entity_id,
        composite_score=round(record.composite_score, 4),
        tier=record.tier.value,
        composite_confidence=round(record.composite_confidence, 4),
        dimension_scores={
            dim.value: {
                "score": round(ds.score, 4),
                "confidence": round(ds.confidence, 4),
                "weight": round(ds.weight, 4),
                "coverage": round(ds.coverage, 4),
                "fields_evaluated": ds.fields_evaluated,
                "fields_present": ds.fields_present,
                "missing_count": len(ds.missing_fields),
            }
            for dim, ds in record.dimension_scores.items()
        },
        missing_field_count=record.total_missing,
        gate_critical_missing=[m.field_name for m in record.gate_critical_missing],
        enrichment_triggers=record.enrichment_trigger_fields,
        scored_at=record.scored_at.isoformat(),
        scoring_duration_ms=round(duration, 2),
    )

    return _envelope(
        payload=response.model_dump(),
        operation="score_entity",
        entity_id=req.entity_id,
        domain=req.domain,
    )


@router.post("/batch", response_model=None)
async def score_batch(
    req: BatchScoreRequest,
    engine=Depends(get_score_engine),
    profile_store=Depends(get_profile_store),
):
    """Score a batch of entities."""
    profile = profile_store.get_profile(req.scoring_profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=SCORING_PROFILE_NOT_FOUND)

    result = engine.score_batch(req, profile)

    return _envelope(
        payload={
            "total_entities": result.total_entities,
            "scored_count": result.scored_count,
            "disqualified_count": result.disqualified_count,
            "avg_composite": result.avg_composite,
            "tier_distribution": {k.value: v for k, v in result.tier_distribution.items()},
            "enrichment_triggers": result.enrichment_triggers,
            "scoring_duration_ms": result.scoring_duration_ms,
            "scores": [
                {
                    "entity_id": s.entity_id,
                    "composite_score": round(s.composite_score, 4),
                    "tier": s.tier.value,
                    "missing_count": s.total_missing,
                }
                for s in result.scores
            ],
        },
        operation="score_batch",
        domain=req.domain,
    )


@router.post("/rescore/{entity_id}", response_model=None)
async def rescore_entity(
    entity_id: str,
    scoring_profile_id: str = Query(...),
    domain: str = Query(...),
    engine=Depends(get_score_engine),
    profile_store=Depends(get_profile_store),
):
    """Re-score an entity after enrichment, preserving provenance."""
    profile = profile_store.get_profile(scoring_profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=SCORING_PROFILE_NOT_FOUND)

    record = engine.rescore_entity(entity_id, profile)

    return _envelope(
        payload={
            "entity_id": record.entity_id,
            "composite_score": round(record.composite_score, 4),
            "tier": record.tier.value,
            "missing_count": record.total_missing,
        },
        operation="rescore_entity",
        entity_id=entity_id,
        domain=domain,
    )


# -- Explain Endpoints -----------------------------------------


@router.post("/explain", response_model=None)
async def explain_score(
    req: ExplainRequest,
    explainer=Depends(get_explainer),
    score_store=Depends(get_score_store),
    profile_store=Depends(get_profile_store),
):
    """Get full explanation for an entity score."""
    record = score_store.get_score(req.entity_id, req.domain)
    if record is None:
        raise HTTPException(status_code=404, detail="Score record not found")

    profile = profile_store.get_profile(req.scoring_profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=SCORING_PROFILE_NOT_FOUND)

    explanation = explainer.explain(record, profile)

    return _envelope(
        payload=explanation.to_dict(),
        operation="explain_score",
        entity_id=req.entity_id,
        domain=req.domain,
    )


@router.post("/compare", response_model=None)
async def compare_scores(
    req: CompareScoresRequest,
    explainer=Depends(get_explainer),
    score_store=Depends(get_score_store),
    profile_store=Depends(get_profile_store),
):
    """Compare before/after scores for an entity."""
    before = score_store.get_score(req.entity_id, req.entity_id)
    after = score_store.get_score(req.entity_id, req.entity_id)
    if before is None or after is None:
        raise HTTPException(status_code=404, detail="Score records not found")

    profile = profile_store.get_profile(req.scoring_profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=SCORING_PROFILE_NOT_FOUND)

    comparison = explainer.compare_scores(before, after, profile)

    return _envelope(
        payload=comparison,
        operation="compare_scores",
        entity_id=req.entity_id,
    )


# -- Decay Endpoints -------------------------------------------


@router.post("/decay/preview", response_model=None)
async def preview_decay(
    req: DecayPreviewRequest,
    decay_engine=Depends(get_decay_engine),
    score_store=Depends(get_score_store),
    profile_store=Depends(get_profile_store),
):
    """Preview score decay trajectory for an entity."""
    record = score_store.get_score(req.entity_id, req.domain)
    if record is None:
        raise HTTPException(status_code=404, detail="Score record not found")

    profile = profile_store.get_profile(req.scoring_profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=SCORING_PROFILE_NOT_FOUND)

    previews = decay_engine.preview_decay(record, profile, req.future_days)

    return _envelope(
        payload={
            "entity_id": req.entity_id,
            "current_composite": round(record.composite_score, 4),
            "current_tier": record.tier.value,
            "trajectory": [
                {
                    "days": int((p.applied_at - record.scored_at).total_seconds() / 86400),
                    "composite": round(p.decayed_composite, 4),
                    "tier": p.decayed_tier.value,
                    "tier_changed": p.tier_changed,
                }
                for p in previews
            ],
        },
        operation="decay_preview",
        entity_id=req.entity_id,
        domain=req.domain,
    )


@router.post("/decay/apply", response_model=None)
async def apply_decay_batch(
    req: DecayBatchRequest,
    decay_engine=Depends(get_decay_engine),
    profile_store=Depends(get_profile_store),
):
    """Apply decay to all stale scores in a domain."""
    profile = profile_store.get_profile(req.scoring_profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=SCORING_PROFILE_NOT_FOUND)

    reports = decay_engine.apply_decay_batch(
        domain=req.domain,
        profile=profile,
        max_age_hours=req.max_age_hours,
        limit=req.limit,
    )

    tier_transitions = [r for r in reports if r.tier_changed]

    return _envelope(
        payload={
            "domain": req.domain,
            "total_decayed": len(reports),
            "tier_transitions": len(tier_transitions),
            "transitions": [
                {
                    "entity_id": t.entity_id,
                    "from_tier": t.original_tier.value,
                    "to_tier": t.decayed_tier.value,
                    "composite_before": round(t.original_composite, 4),
                    "composite_after": round(t.decayed_composite, 4),
                }
                for t in tier_transitions[:50]
            ],
            "entities_needing_re_enrichment": [
                r.entity_id for r in reports if r.needs_re_enrichment
            ][:100],
        },
        operation="decay_batch",
        domain=req.domain,
    )


# -- Profile Endpoints -----------------------------------------


@router.post("/profile", response_model=None)
async def create_profile(
    req: ProfileCreateRequest,
    profile_store=Depends(get_profile_store),
):
    """Create a new scoring profile."""
    weights = {}
    if req.dimension_weights:
        for k, v in req.dimension_weights.items():
            try:
                dim = ScoreDimension(k)
                weights[dim] = v
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid dimension: {k}") from None

    thresholds = {}
    if req.tier_thresholds:
        for k, v in req.tier_thresholds.items():
            try:
                tier = ScoreTier(k)
                thresholds[tier] = v
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid tier: {k}") from None

    profile = ScoringProfile(
        name=req.name,
        domain=req.domain,
        description=req.description,
        confidence_floor=req.confidence_floor,
        gate_penalty=req.gate_penalty,
        min_fields_for_score=req.min_fields_for_score,
    )
    if weights:
        profile.dimension_weights = weights
    if thresholds:
        profile.tier_thresholds = thresholds

    profile_store.save_profile(profile)

    return _envelope(
        payload={
            "profile_id": profile.profile_id,
            "name": profile.name,
            "domain": profile.domain,
            "weights": {k.value: v for k, v in profile.dimension_weights.items()},
            "tier_thresholds": {k.value: v for k, v in profile.tier_thresholds.items()},
        },
        operation="create_profile",
        domain=req.domain,
    )


@router.get("/profile/{profile_id}", response_model=None)
async def get_profile(
    profile_id: str,
    profile_store=Depends(get_profile_store),
):
    """Retrieve a scoring profile."""
    profile = profile_store.get_profile(profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    return _envelope(
        payload=profile.model_dump(mode="json"),
        operation="get_profile",
        domain=profile.domain,
    )


# -- Tier Distribution -----------------------------------------


@router.get("/distribution/{domain}", response_model=None)
async def get_tier_distribution(
    domain: str,
    score_store=Depends(get_score_store),
):
    """Get tier distribution for all scored entities in a domain."""
    all_scores = score_store.list_scores(domain, tier=None, limit=10000)
    distribution: dict[str, int] = {}
    total_composite = 0.0

    for s in all_scores:
        tier_key = s.tier.value
        distribution[tier_key] = distribution.get(tier_key, 0) + 1
        total_composite += s.composite_score

    avg = total_composite / len(all_scores) if all_scores else 0.0

    return _envelope(
        payload=TierDistributionResponse(
            domain=domain,
            total=len(all_scores),
            distribution=distribution,
            avg_composite=round(avg, 4),
            generated_at=datetime.now(UTC).isoformat(),
        ).model_dump(),
        operation="tier_distribution",
        domain=domain,
    )
