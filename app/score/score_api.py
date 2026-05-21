"""
SCORE Service — FastAPI Router
revopsos-score-engine

REST endpoints for scoring, decay, and explainability.
All endpoints produce TransportPacket-compatible responses with
full provenance and downstream routing metadata.

L9 Architecture Note:
    ``app/score/score_api.py`` is a CHASSIS transport adapter.
    FastAPI imports are permitted here because this module owns HTTP ingress
    and dependency injection boundaries before execution enters engine logic.

# L9-node: enrichment-inference-engine
# L9-layer: chassis
# L9-contract-version: 1.0.0
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

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
