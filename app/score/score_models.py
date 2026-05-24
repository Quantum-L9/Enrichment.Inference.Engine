"""
SCORE Service — Data Models
revopsos-score-engine models

Canonical types for multi-dimensional lead/deal scoring.
Consumed by score_engine.py, score_decay.py, score_explainer.py, score_api.py.

# L9-fix: NAME-FLAT (FALSE_POSITIVE)
# L9-file: app/score/score_models.py
# L9-violation: NAME-FLAT fired on `recommendation: RecommendationType`
# L9-fix-summary: `recommendation` is a standard English word in valid snake_case.
#   Added # l9:flatcase-ok annotation per audit FP suppression protocol.
# L9-layer: engine/model
# L9-node: enrichment-inference-engine
# L9-contract-version: 1.0.0
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# ── Enums ──────────────────────────────────────────────────


class ScoreDimension(StrEnum):
    FIT = "fit"
    INTENT = "intent"
    ENGAGEMENT = "engagement"
    READINESS = "readiness"
    GRAPH_AFFINITY = "graph_affinity"


class ScoreSource(StrEnum):
    ENRICHMENT = "enrichment"
    GRAPH = "graph"
    SIGNAL = "signal"
    CRM = "crm"
    INFERENCE = "inference"


class ScoreTier(StrEnum):
    HOT = "hot"
    WARM = "warm"
    COOL = "cool"
    COLD = "cold"
    DISQUALIFIED = "disqualified"


class ICPFieldType(StrEnum):
    EXACT_MATCH = "exact_match"
    RANGE = "range"
    CONTAINS = "contains"
    BOOLEAN = "boolean"
    WEIGHTED_SET = "weighted_set"


class RecommendationType(StrEnum):
    ENRICH_FIELD = "enrich_field"
    VERIFY_FIELD = "verify_field"
    CAPTURE_SIGNAL = "capture_signal"
    RE_SCORE = "re_score"
    MANUAL_REVIEW = "manual_review"


# ── Field Contribution ──────────────────────────────────────────


class FieldContribution(BaseModel):
    """How a single enriched field contributed to a dimension score."""

    field_name: str
    dimension: ScoreDimension
    raw_value: Any = None
    contribution: float = Field(ge=0.0, le=1.0, description="0.0-1.0 contribution weight")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence of the underlying field")
    source: ScoreSource = ScoreSource.ENRICHMENT
    match_type: ICPFieldType = ICPFieldType.EXACT_MATCH
    match_detail: str = ""


class MissingField(BaseModel):
    """A field the scoring profile expected but the entity lacked."""

    field_name: str
    dimension: ScoreDimension
    impact_estimate: float = Field(
        ge=0.0, le=1.0, description="Estimated score improvement if this field were present"
    )
    is_gate_critical: bool = False
    recommendation: RecommendationType = RecommendationType.ENRICH_FIELD  # l9:flatcase-ok -- single English word


# ── Dimension Score ────────────────────────────────────────────


class DimensionScore(BaseModel):
    """Score for a single dimension with full provenance."""

    dimension: ScoreDimension
    score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    weight: float = Field(ge=0.0, le=1.0, description="Weight in composite calculation")
    field_contributions: list[FieldContribution] = Field(default_factory=list)
    missing_fields: list[MissingField] = Field(default_factory=list)
    fields_evaluated: int = 0
    fields_present: int = 0
    coverage: float = Field(
        ge=0.0, le=1.0, default=0.0, description="fields_present / fields_evaluated"
    )
    decayed_score: float | None = None
    decay_factor: float = 1.0


# ── Score Record ─────────────────────────────────────────────


class ScoreProvenance(BaseModel):
    """Full provenance chain for a score."""

    enrichment_run_id: str | None = None
    graph_match_id: str | None = None
    signal_ids: list[str] = Field(default_factory=list)
    scoring_profile_id: str = ""
    scoring_profile_version: str = ""
    scored_by: str = "revopsos-score-engine"
    model_version: str = "1.0.0"


class ScoreRecord(BaseModel):
    """Complete scoring output for a single entity."""

    score_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    entity_id: str
    domain: str
    dimension_scores: dict[ScoreDimension, DimensionScore] = Field(default_factory=dict)
    composite_score: float = Field(ge=0.0, le=1.0, default=0.0)
    composite_confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    tier: ScoreTier = ScoreTier.COLD
    field_contributions: list[FieldContribution] = Field(default_factory=list)
    missing_fields: list[MissingField] = Field(default_factory=list)
    scored_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    decayed_composite: float | None = None
    decay_applied_at: datetime | None = None
    provenance: ScoreProvenance = Field(default_factory=ScoreProvenance)

    @property
    def total_missing(self) -> int:
        return len(self.missing_fields)

    @property
    def gate_critical_missing(self) -> list[MissingField]:
        return [m for m in self.missing_fields if m.is_gate_critical]

    @property
    def enrichment_trigger_fields(self) -> list[str]:
        return [m.field_name for m in self.missing_fields if m.impact_estimate >= 0.10]


# ── ICP Definition ─────────────────────────────────────────────


class ICPFieldCriterion(BaseModel):
    """Single field criterion in an ICP definition."""

    field_name: str
    field_type: ICPFieldType
    target_value: Any = None
    target_range: tuple[float, float] | None = None
    target_set: list[Any] | None = None
    set_weights: dict[str, float] | None = None
    weight: float = Field(ge=0.0, le=1.0, default=0.5)
    is_gate_critical: bool = False
    dimension: ScoreDimension = ScoreDimension.FIT
    description: str = ""


class ICPDefinition(BaseModel):
    """Domain-specific Ideal Customer Profile."""

    icp_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    domain: str
    description: str = ""
    criteria: list[ICPFieldCriterion] = Field(default_factory=list)
    version: str = "1.0.0"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def gate_criteria(self) -> list[ICPFieldCriterion]:
        return [c for c in self.criteria if c.is_gate_critical]

    @property
    def criteria_by_dimension(self) -> dict[ScoreDimension, list[ICPFieldCriterion]]:
        result: dict[ScoreDimension, list[ICPFieldCriterion]] = {}
        for c in self.criteria:
            result.setdefault(c.dimension, []).append(c)
        return result


# ── Scoring Profile ─────────────────────────────────────────────


class DecayConfig(BaseModel):
    """Per-dimension decay configuration."""

    dimension: ScoreDimension
    half_life_days: float = 30.0
    min_score: float = 0.0
    max_decay: float = 0.95


DEFAULT_WEIGHTS: dict[ScoreDimension, float] = {
    ScoreDimension.FIT: 0.30,
    ScoreDimension.INTENT: 0.25,
    ScoreDimension.ENGAGEMENT: 0.20,
    ScoreDimension.READINESS: 0.15,
    ScoreDimension.GRAPH_AFFINITY: 0.10,
}

DEFAULT_DECAY_CONFIGS: list[DecayConfig] = [
    DecayConfig(dimension=ScoreDimension.FIT, half_life_days=180.0),
    DecayConfig(dimension=ScoreDimension.INTENT, half_life_days=14.0),
    DecayConfig(dimension=ScoreDimension.ENGAGEMENT, half_life_days=30.0),
    DecayConfig(dimension=ScoreDimension.READINESS, half_life_days=7.0),
    DecayConfig(dimension=ScoreDimension.GRAPH_AFFINITY, half_life_days=90.0),
]

DEFAULT_TIER_THRESHOLDS: dict[ScoreTier, float]:
DEFAULT_TIER_THRESHOLDS = {
    ScoreTier.HOT: 0.80,
    ScoreTier.WARM: 0.60,
    ScoreTier.COOL: 0.40,
    ScoreTier.COLD: 0.20,
    ScoreTier.DISQUALIFIED: 0.0,
}


class ScoringProfile(BaseModel):
    """Customer-configurable scoring profile."""

    profile_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    domain: str
    description: str = ""
    version: str = "1.0.0"

    dimension_weights: dict[ScoreDimension, float] = Field(
        default_factory=lambda: dict(DEFAULT_WEIGHTS)
    )
    decay_configs: list[DecayConfig] = Field(default_factory=lambda: list(DEFAULT_DECAY_CONFIGS))
    tier_thresholds: dict[ScoreTier, float] = Field(
        default_factory=lambda: dict(DEFAULT_TIER_THRESHOLDS)
    )
    icp: ICPDefinition | None = None

    confidence_floor: float = Field(
        ge=0.0,
        le=1.0,
        default=0.30,
        description="Minimum confidence to include a field contribution",
    )
    gate_penalty: float = Field(
        ge=0.0,
        le=1.0,
        default=0.50,
        description="Maximum composite score when gate-critical fields are missing",
    )
    min_fields_for_score: int = Field(
        ge=1, default=3, description="Minimum present fields to produce a score (else disqualified)"
    )

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def get_weight(self, dimension: ScoreDimension) -> float:
        return self.dimension_weights.get(dimension, 0.0)

    def get_decay_config(self, dimension: ScoreDimension) -> DecayConfig:
        for dc in self.decay_configs:
            if dc.dimension == dimension:
                return dc
        return DecayConfig(dimension=dimension, half_life_days=30.0)

    def classify_tier(self, composite_score: float) -> ScoreTier:
        sorted_tiers = sorted(self.tier_thresholds.items(), key=lambda x: x[1], reverse=True)
        for tier, threshold in sorted_tiers:
            if composite_score >= threshold:
                return tier
        return ScoreTier.DISQUALIFIED

    @property
    def weights_sum(self) -> float:
        return sum(self.dimension_weights.values())

    def normalized_weight(self, dimension: ScoreDimension) -> float:
        total = self.weights_sum
        if total == 0:
            return 0.0
        return self.dimension_weights.get(dimension, 0.0) / total


# ── Batch Scoring ─────────────────────────────────────────────


class BatchScoreRequest(BaseModel):
    entity_ids: list[str]
    scoring_profile_id: str
    domain: str
    include_explanations: bool = False


class BatchScoreResponse(BaseModel):
    scores: list[ScoreRecord] = Field(default_factory=list)
    total_entities: int = 0
    scored_count: int = 0
    disqualified_count: int = 0
    avg_composite: float = 0.0
    tier_distribution: dict[ScoreTier, int] = Field(default_factory=dict)
    scoring_duration_ms: float = 0.0
    enrichment_triggers: list[str] = Field(
        default_factory=list,
        description="Entity IDs that should be re-enriched based on missing fields",
    )


# ── Helpers ───────────────────────────────────────────────────


def compute_decay_factor(
    days_elapsed: float,
    half_life_days: float,
    min_score: float = 0.0,
    max_decay: float = 0.95,
) -> float:
    """Exponential decay: factor = 2^(-days/half_life), clamped."""
    if half_life_days <= 0:
        return min_score
    if days_elapsed <= 0:
        return 1.0
    raw = math.pow(2.0, -days_elapsed / half_life_days)
    decayed = max(raw, 1.0 - max_decay)
    return max(decayed, min_score)


def compute_composite(
    dimension_scores: dict[ScoreDimension, DimensionScore],
    profile: ScoringProfile,
    use_decayed: bool = False,
) -> tuple[float, float]:
    """
    Weighted composite score and confidence.
    Returns (composite_score, composite_confidence).
    """
    weighted_score = 0.0
    weighted_confidence = 0.0
    total_weight = 0.0

    for dim, ds in dimension_scores.items():
        w = profile.normalized_weight(dim)
        score_val = ds.decayed_score if (use_decayed and ds.decayed_score is not None) else ds.score
        weighted_score += score_val * w
        weighted_confidence += ds.confidence * w
        total_weight += w

    if total_weight == 0:
        return 0.0, 0.0

    composite = weighted_score / total_weight * total_weight
    confidence = weighted_confidence / total_weight * total_weight
    return min(composite, 1.0), min(confidence, 1.0)


def apply_gate_penalty(
    composite: float,
    missing_fields: list[MissingField],
    penalty_cap: float = 0.50,
) -> float:
    """Cap composite score if gate-critical fields are missing."""
    gate_missing = [m for m in missing_fields if m.is_gate_critical]
    if gate_missing:
        return min(composite, penalty_cap)
    return composite
