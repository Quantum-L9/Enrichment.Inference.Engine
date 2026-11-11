"""
SCORE Service — Core Scoring Engine
revopsos-score-engine

Multi-dimensional entity scoring with domain-aware ICP matching,
graph affinity integration, and full field-level provenance.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from .score_models import (
    BatchScoreRequest,
    BatchScoreResponse,
    DimensionScore,
    FieldContribution,
    ICPDefinition,
    ICPFieldCriterion,
    ICPFieldType,
    MissingField,
    RecommendationType,
    ScoreDimension,
    ScoreProvenance,
    ScoreRecord,
    ScoreSource,
    ScoreTier,
    ScoringProfile,
    apply_gate_penalty,
    compute_composite,
)

# ── Protocols ─────────────────────────────────────────────────


@runtime_checkable
class EntityDataProvider(Protocol):
    """Provides enriched entity data for scoring."""

    def get_entity_fields(self, entity_id: str, domain: str) -> dict[str, Any]: ...
    def get_field_confidences(self, entity_id: str) -> dict[str, float]: ...
    def get_field_sources(self, entity_id: str) -> dict[str, str]: ...


@runtime_checkable
class GraphAffinityProvider(Protocol):
    """Provides graph match scores from GRAPH engine."""

    def get_graph_affinity(self, entity_id: str, domain: str) -> float: ...
    def get_community_id(self, entity_id: str) -> str | None: ...
    def get_match_rank(self, entity_id: str) -> int | None: ...


@runtime_checkable
class SignalProvider(Protocol):
    """Provides engagement/intent signals from SIGNAL service."""

    def get_engagement_score(self, entity_id: str) -> float: ...
    def get_intent_score(self, entity_id: str) -> float: ...
    def get_readiness_score(self, entity_id: str) -> float: ...
    def get_last_signal_at(self, entity_id: str) -> datetime | None: ...


@runtime_checkable
class ProfileStore(Protocol):
    """Stores and retrieves scoring profiles."""

    def get_profile(self, profile_id: str) -> ScoringProfile | None: ...
    def save_profile(self, profile: ScoringProfile) -> None: ...
    def list_profiles(self, domain: str) -> list[ScoringProfile]: ...


@runtime_checkable
class ScoreStore(Protocol):
    """Persists score records."""

    def save_score(self, record: ScoreRecord) -> None: ...
    def get_score(self, entity_id: str, domain: str) -> ScoreRecord | None: ...
    # One stored record by its own `ScoreRecord.score_id`, not the latest for
    # an entity. `/score/compare` names the two records it compares this way.
    def get_score_by_id(self, score_id: str) -> ScoreRecord | None: ...
    def get_scores_batch(self, entity_ids: list[str], domain: str) -> list[ScoreRecord]: ...
    def list_scores(self, domain: str, tier: ScoreTier | None, limit: int) -> list[ScoreRecord]: ...


# ── ICP Field Matching ────────────────────────────────────────


def _eval_exact_match(criterion: ICPFieldCriterion, value: Any) -> float:
    """Score EXACT_MATCH criterion: case-insensitive string equality or direct equality."""
    if isinstance(value, str) and isinstance(criterion.target_value, str):
        return 1.0 if value.strip().lower() == criterion.target_value.strip().lower() else 0.0
    return 1.0 if value == criterion.target_value else 0.0


def _eval_range(criterion: ICPFieldCriterion, value: Any) -> float:
    """Score RANGE criterion: full credit in range, proximity-decay outside."""
    if criterion.target_range is None:
        return 0.0
    low, high = criterion.target_range
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    if low <= num <= high:
        return 1.0
    range_span = high - low
    if range_span == 0:
        return 0.0
    distance = (low - num) if num < low else (num - high)
    return max(0.0, 1.0 - (distance / range_span))


def _eval_contains(criterion: ICPFieldCriterion, value: Any) -> float:
    """Score CONTAINS criterion: list-overlap ratio or substring match."""
    if isinstance(value, list):
        target = criterion.target_value
        if isinstance(target, list):
            matched = sum(1 for t in target if t in value)
            return matched / len(target) if target else 0.0
        return 1.0 if target in value else 0.0
    if isinstance(value, str) and isinstance(criterion.target_value, str):
        return 1.0 if criterion.target_value.lower() in value.lower() else 0.0
    return 0.0


def _eval_boolean(criterion: ICPFieldCriterion, value: Any) -> float:
    """Score BOOLEAN criterion: 1.0 if bool(value) matches target, else 0.0."""
    try:
        bool_val = bool(value)
    except (TypeError, ValueError):
        return 0.0
    return 1.0 if bool_val == bool(criterion.target_value) else 0.0


def _eval_weighted_set(criterion: ICPFieldCriterion, value: Any) -> float:
    """Score WEIGHTED_SET criterion: weighted overlap against set_weights map."""
    if criterion.target_set is None or criterion.set_weights is None:
        return 0.0
    if isinstance(value, list):
        total_weight = sum(criterion.set_weights.values())
        if total_weight == 0:
            return 0.0
        matched_weight = sum(
            criterion.set_weights.get(str(v), 0.0) for v in value if str(v) in criterion.set_weights
        )
        return min(matched_weight / total_weight, 1.0)
    return criterion.set_weights.get(str(value), 0.0)


_CRITERION_EVALUATORS = {
    ICPFieldType.EXACT_MATCH: _eval_exact_match,
    ICPFieldType.RANGE: _eval_range,
    ICPFieldType.CONTAINS: _eval_contains,
    ICPFieldType.BOOLEAN: _eval_boolean,
    ICPFieldType.WEIGHTED_SET: _eval_weighted_set,
}


def _evaluate_criterion(
    criterion: ICPFieldCriterion,
    value: Any,
) -> float:
    """Evaluate a single ICP criterion against a field value. Returns 0.0-1.0 match score."""
    if value is None:
        return 0.0
    evaluator = _CRITERION_EVALUATORS.get(criterion.field_type)
    return evaluator(criterion, value) if evaluator else 0.0


# ── Dimension Scorers ─────────────────────────────────────────


def _missing_fit_field(criterion: ICPFieldCriterion, total_weight: float) -> MissingField:
    """Build a MissingField entry for a criterion that cannot be scored."""
    impact = criterion.weight / max(total_weight, 1.0)
    return MissingField(
        field_name=criterion.field_name,
        dimension=ScoreDimension.FIT,
        impact_estimate=min(impact, 1.0),
        is_gate_critical=criterion.is_gate_critical,
        recommendation=RecommendationType.ENRICH_FIELD,
    )


def _fit_field_contribution(
    criterion: ICPFieldCriterion,
    value: Any,
    conf: float,
    total_weight: float,
    source_str: str,
) -> tuple[FieldContribution, float]:
    """Score one FIT criterion and return (contribution, weighted_score)."""
    match_score = _evaluate_criterion(criterion, value)
    weighted = match_score * conf * criterion.weight
    try:
        source = ScoreSource(source_str)
    except ValueError:
        source = ScoreSource.ENRICHMENT
    contribution = FieldContribution(
        field_name=criterion.field_name,
        dimension=ScoreDimension.FIT,
        raw_value=value,
        contribution=min(weighted / max(total_weight, 0.01), 1.0),
        confidence=conf,
        source=source,
        match_type=criterion.field_type,
        match_detail=criterion.description or f"ICP match: {criterion.field_name}",
    )
    return contribution, weighted


def score_fit(
    entity_fields: dict[str, Any],
    field_confidences: dict[str, float],
    field_sources: dict[str, str],
    icp: ICPDefinition | None,
    profile: ScoringProfile,
) -> DimensionScore:
    """Score entity fit against ICP definition."""
    contributions: list[FieldContribution] = []
    missing: list[MissingField] = []
    fit_criteria = icp.criteria_by_dimension.get(ScoreDimension.FIT, []) if icp else []

    if not fit_criteria:
        return DimensionScore(
            dimension=ScoreDimension.FIT,
            score=0.0,
            confidence=0.0,
            weight=profile.get_weight(ScoreDimension.FIT),
            fields_evaluated=0,
            fields_present=0,
            coverage=0.0,
        )

    total_weighted_score = 0.0
    total_weight = 0.0
    fields_present = 0

    for criterion in fit_criteria:
        total_weight += criterion.weight
        value = entity_fields.get(criterion.field_name)
        conf = field_confidences.get(criterion.field_name, 0.0)

        if value is None or conf < profile.confidence_floor:
            missing.append(_missing_fit_field(criterion, total_weight))
            continue

        fields_present += 1
        contrib, weighted = _fit_field_contribution(
            criterion,
            value,
            conf,
            total_weight,
            field_sources.get(criterion.field_name, "enrichment"),
        )
        contributions.append(contrib)
        total_weighted_score += weighted

    raw_score = total_weighted_score / total_weight if total_weight > 0 else 0.0
    avg_conf = (
        sum(c.confidence for c in contributions) / len(contributions) if contributions else 0.0
    )
    coverage = fields_present / len(fit_criteria) if fit_criteria else 0.0

    return DimensionScore(
        dimension=ScoreDimension.FIT,
        score=min(raw_score, 1.0),
        confidence=avg_conf,
        weight=profile.get_weight(ScoreDimension.FIT),
        field_contributions=contributions,
        missing_fields=missing,
        fields_evaluated=len(fit_criteria),
        fields_present=fields_present,
        coverage=coverage,
    )


def score_intent(
    signal_provider: SignalProvider | None,
    entity_id: str,
    profile: ScoringProfile,
) -> DimensionScore:
    """Score entity intent from signal data."""
    missing: list[MissingField] = []

    if signal_provider is None:
        missing.append(
            MissingField(
                field_name="intent_signals",
                dimension=ScoreDimension.INTENT,
                impact_estimate=0.50,
                recommendation=RecommendationType.CAPTURE_SIGNAL,
            )
        )
        return DimensionScore(
            dimension=ScoreDimension.INTENT,
            score=0.0,
            confidence=0.0,
            weight=profile.get_weight(ScoreDimension.INTENT),
            missing_fields=missing,
        )

    intent = signal_provider.get_intent_score(entity_id)
    last_signal = signal_provider.get_last_signal_at(entity_id)
    confidence = 0.70 if last_signal else 0.30

    contributions = [
        FieldContribution(
            field_name="intent_score",
            dimension=ScoreDimension.INTENT,
            raw_value=intent,
            contribution=intent,
            confidence=confidence,
            source=ScoreSource.SIGNAL,
            match_detail="Aggregated intent signals (multi-stakeholder, pricing page, etc.)",
        )
    ]

    return DimensionScore(
        dimension=ScoreDimension.INTENT,
        score=min(intent, 1.0),
        confidence=confidence,
        weight=profile.get_weight(ScoreDimension.INTENT),
        field_contributions=contributions,
        fields_evaluated=1,
        fields_present=1 if intent > 0 else 0,
        coverage=1.0 if intent > 0 else 0.0,
    )


def score_engagement(
    signal_provider: SignalProvider | None,
    entity_id: str,
    profile: ScoringProfile,
) -> DimensionScore:
    """Score entity engagement from signal data."""
    missing: list[MissingField] = []

    if signal_provider is None:
        missing.append(
            MissingField(
                field_name="engagement_signals",
                dimension=ScoreDimension.ENGAGEMENT,
                impact_estimate=0.40,
                recommendation=RecommendationType.CAPTURE_SIGNAL,
            )
        )
        return DimensionScore(
            dimension=ScoreDimension.ENGAGEMENT,
            score=0.0,
            confidence=0.0,
            weight=profile.get_weight(ScoreDimension.ENGAGEMENT),
            missing_fields=missing,
        )

    engagement = signal_provider.get_engagement_score(entity_id)
    last_signal = signal_provider.get_last_signal_at(entity_id)
    confidence = 0.75 if last_signal else 0.25

    contributions = [
        FieldContribution(
            field_name="engagement_score",
            dimension=ScoreDimension.ENGAGEMENT,
            raw_value=engagement,
            contribution=engagement,
            confidence=confidence,
            source=ScoreSource.SIGNAL,
            match_detail="Rolling 30d decay-weighted engagement score",
        )
    ]

    return DimensionScore(
        dimension=ScoreDimension.ENGAGEMENT,
        score=min(engagement, 1.0),
        confidence=confidence,
        weight=profile.get_weight(ScoreDimension.ENGAGEMENT),
        field_contributions=contributions,
        fields_evaluated=1,
        fields_present=1 if engagement > 0 else 0,
        coverage=1.0 if engagement > 0 else 0.0,
    )


def score_readiness(
    signal_provider: SignalProvider | None,
    entity_id: str,
    entity_fields: dict[str, Any],
    field_confidences: dict[str, float],
    profile: ScoringProfile,
) -> DimensionScore:
    """Score entity readiness from signals + field completeness."""
    contributions: list[FieldContribution] = []
    missing: list[MissingField] = []
    components: list[float] = []

    if signal_provider is not None:
        readiness_signal = signal_provider.get_readiness_score(entity_id)
        components.append(readiness_signal)
        contributions.append(
            FieldContribution(
                field_name="readiness_signal",
                dimension=ScoreDimension.READINESS,
                raw_value=readiness_signal,
                contribution=readiness_signal * 0.5,
                confidence=0.70,
                source=ScoreSource.SIGNAL,
                match_detail="Signal-derived readiness (deal stage, proposal views, etc.)",
            )
        )
    else:
        missing.append(
            MissingField(
                field_name="readiness_signals",
                dimension=ScoreDimension.READINESS,
                impact_estimate=0.30,
                recommendation=RecommendationType.CAPTURE_SIGNAL,
            )
        )

    if field_confidences:
        avg_conf = sum(field_confidences.values()) / len(field_confidences)
        completeness = sum(1 for v in entity_fields.values() if v is not None) / max(
            len(entity_fields), 1
        )
        data_readiness = avg_conf * 0.6 + completeness * 0.4
        components.append(data_readiness)
        contributions.append(
            FieldContribution(
                field_name="data_readiness",
                dimension=ScoreDimension.READINESS,
                raw_value=data_readiness,
                contribution=data_readiness * 0.5,
                confidence=avg_conf,
                source=ScoreSource.ENRICHMENT,
                match_detail=f"Data completeness ({completeness:.0%}) x confidence ({avg_conf:.2f})",
            )
        )

    score = sum(components) / len(components) if components else 0.0
    confidence = 0.60 if components else 0.0

    return DimensionScore(
        dimension=ScoreDimension.READINESS,
        score=min(score, 1.0),
        confidence=confidence,
        weight=profile.get_weight(ScoreDimension.READINESS),
        field_contributions=contributions,
        missing_fields=missing,
        fields_evaluated=2,
        fields_present=len(components),
        coverage=len(components) / 2.0,
    )


def score_graph_affinity(
    graph_provider: GraphAffinityProvider | None,
    entity_id: str,
    domain: str,
    profile: ScoringProfile,
) -> DimensionScore:
    """Score entity graph affinity from GRAPH engine."""
    missing: list[MissingField] = []

    if graph_provider is None:
        missing.append(
            MissingField(
                field_name="graph_affinity",
                dimension=ScoreDimension.GRAPH_AFFINITY,
                impact_estimate=0.20,
                recommendation=RecommendationType.RE_SCORE,
            )
        )
        return DimensionScore(
            dimension=ScoreDimension.GRAPH_AFFINITY,
            score=0.0,
            confidence=0.0,
            weight=profile.get_weight(ScoreDimension.GRAPH_AFFINITY),
            missing_fields=missing,
        )

    affinity = graph_provider.get_graph_affinity(entity_id, domain)
    community = graph_provider.get_community_id(entity_id)
    rank = graph_provider.get_match_rank(entity_id)
    confidence = 0.85 if community else 0.50

    contributions = [
        FieldContribution(
            field_name="graph_affinity_score",
            dimension=ScoreDimension.GRAPH_AFFINITY,
            raw_value=affinity,
            contribution=affinity,
            confidence=confidence,
            source=ScoreSource.GRAPH,
            match_detail=f"Graph match rank={rank}, community={community}",
        )
    ]

    return DimensionScore(
        dimension=ScoreDimension.GRAPH_AFFINITY,
        score=min(affinity, 1.0),
        confidence=confidence,
        weight=profile.get_weight(ScoreDimension.GRAPH_AFFINITY),
        field_contributions=contributions,
        fields_evaluated=1,
        fields_present=1 if affinity > 0 else 0,
        coverage=1.0 if affinity > 0 else 0.0,
    )


# ── Core Scoring Engine ──────────────────────────────────────


class ScoreEngine:
    """
    Multi-dimensional entity scoring engine.

    Scores entities across 5 dimensions (fit, intent, engagement,
    readiness, graph_affinity) using enriched fields, graph data,
    and engagement signals. Produces fully explainable ScoreRecords
    with field-level provenance.
    """

    def __init__(
        self,
        entity_provider: EntityDataProvider,
        profile_store: ProfileStore,
        score_store: ScoreStore,
        graph_provider: GraphAffinityProvider | None = None,
        signal_provider: SignalProvider | None = None,
    ):
        self._entity_provider = entity_provider
        self._profile_store = profile_store
        self._score_store = score_store
        self._graph_provider = graph_provider
        self._signal_provider = signal_provider

    def score_entity(
        self,
        entity_id: str,
        profile: ScoringProfile,
        enrichment_run_id: str | None = None,
        graph_match_id: str | None = None,
    ) -> ScoreRecord:
        """Score a single entity across all dimensions."""
        entity_fields = self._entity_provider.get_entity_fields(entity_id, profile.domain)
        field_confidences = self._entity_provider.get_field_confidences(entity_id)
        field_sources = self._entity_provider.get_field_sources(entity_id)

        present_count = sum(1 for v in entity_fields.values() if v is not None)
        if present_count < profile.min_fields_for_score:
            return self._disqualified_record(entity_id, profile, entity_fields)

        fit = score_fit(entity_fields, field_confidences, field_sources, profile.icp, profile)
        intent = score_intent(self._signal_provider, entity_id, profile)
        engagement = score_engagement(self._signal_provider, entity_id, profile)
        readiness = score_readiness(
            self._signal_provider, entity_id, entity_fields, field_confidences, profile
        )
        graph_aff = score_graph_affinity(self._graph_provider, entity_id, profile.domain, profile)

        dimension_scores = {
            ScoreDimension.FIT: fit,
            ScoreDimension.INTENT: intent,
            ScoreDimension.ENGAGEMENT: engagement,
            ScoreDimension.READINESS: readiness,
            ScoreDimension.GRAPH_AFFINITY: graph_aff,
        }

        composite, composite_conf = compute_composite(dimension_scores, profile)

        all_missing: list[MissingField] = []
        all_contributions: list[FieldContribution] = []
        for ds in dimension_scores.values():
            all_missing.extend(ds.missing_fields)
            all_contributions.extend(ds.field_contributions)

        composite = apply_gate_penalty(composite, all_missing, profile.gate_penalty)
        tier = profile.classify_tier(composite)

        provenance = ScoreProvenance(
            enrichment_run_id=enrichment_run_id,
            graph_match_id=graph_match_id,
            scoring_profile_id=profile.profile_id,
            scoring_profile_version=profile.version,
        )

        record = ScoreRecord(
            entity_id=entity_id,
            domain=profile.domain,
            dimension_scores=dimension_scores,
            composite_score=composite,
            composite_confidence=composite_conf,
            tier=tier,
            field_contributions=all_contributions,
            missing_fields=all_missing,
            provenance=provenance,
        )

        self._score_store.save_score(record)
        return record

    def score_batch(
        self,
        request: BatchScoreRequest,
        profile: ScoringProfile | None = None,
    ) -> BatchScoreResponse:
        """Score a batch of entities."""
        start = time.monotonic()

        if profile is None:
            profile = self._profile_store.get_profile(request.scoring_profile_id)
            if profile is None:
                return BatchScoreResponse(
                    total_entities=len(request.entity_ids),
                    scored_count=0,
                )

        scores: list[ScoreRecord] = []
        tier_dist: dict[ScoreTier, int] = {}
        disqualified = 0
        trigger_entities: list[str] = []

        for eid in request.entity_ids:
            record = self.score_entity(eid, profile)
            scores.append(record)

            tier_dist[record.tier] = tier_dist.get(record.tier, 0) + 1
            if record.tier == ScoreTier.DISQUALIFIED:
                disqualified += 1

            if record.enrichment_trigger_fields:
                trigger_entities.append(eid)

        scored_count = len(scores) - disqualified
        avg_composite = sum(s.composite_score for s in scores) / len(scores) if scores else 0.0
        duration = (time.monotonic() - start) * 1000

        return BatchScoreResponse(
            scores=scores,
            total_entities=len(request.entity_ids),
            scored_count=scored_count,
            disqualified_count=disqualified,
            avg_composite=round(avg_composite, 4),
            tier_distribution=tier_dist,
            scoring_duration_ms=round(duration, 2),
            enrichment_triggers=trigger_entities,
        )

    def rescore_entity(
        self,
        entity_id: str,
        profile: ScoringProfile,
    ) -> ScoreRecord:
        """Re-score entity, preserving previous score provenance."""
        previous = self._score_store.get_score(entity_id, profile.domain)
        new_record = self.score_entity(entity_id, profile)

        if previous:
            new_record.provenance.enrichment_run_id = (
                new_record.provenance.enrichment_run_id or previous.provenance.enrichment_run_id
            )
            new_record.provenance.signal_ids = list(
                set(previous.provenance.signal_ids + new_record.provenance.signal_ids)
            )

        return new_record

    def _disqualified_record(
        self,
        entity_id: str,
        profile: ScoringProfile,
        entity_fields: dict[str, Any],
    ) -> ScoreRecord:
        """Produce a disqualified score for entities below minimum field threshold."""
        present = [k for k, v in entity_fields.items() if v is not None]
        all_expected = set()
        if profile.icp:
            all_expected = {c.field_name for c in profile.icp.criteria}
        missing_names = all_expected - set(present)

        missing = [
            MissingField(
                field_name=fn,
                dimension=ScoreDimension.FIT,
                impact_estimate=0.50,
                is_gate_critical=True,
                recommendation=RecommendationType.ENRICH_FIELD,
            )
            for fn in missing_names
        ]

        record = ScoreRecord(
            entity_id=entity_id,
            domain=profile.domain,
            composite_score=0.0,
            composite_confidence=0.0,
            tier=ScoreTier.DISQUALIFIED,
            missing_fields=missing,
            provenance=ScoreProvenance(
                scoring_profile_id=profile.profile_id,
                scoring_profile_version=profile.version,
            ),
        )
        self._score_store.save_score(record)
        return record
