"""
SCORE Service — Score Decay Engine
revopsos-score-engine

Applies temporal decay to dimension scores based on configurable
per-dimension half-lives. Scores degrade if entities are not
re-enriched or re-engaged.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from app.utils.safe_convert import safe_float

from .score_models import (
    ScoreDimension,
    ScoreRecord,
    ScoreTier,
    ScoringProfile,
    apply_gate_penalty,
    compute_composite,
    compute_decay_factor,
)

# ── Protocols ─────────────────────────────────────────────────


@runtime_checkable
class DecayScoreStore(Protocol):
    """Score store with batch retrieval for decay processing."""

    def get_score(self, entity_id: str, domain: str) -> ScoreRecord | None: ...
    def save_score(self, record: ScoreRecord) -> None: ...
    def list_scores_needing_decay(
        self, domain: str, max_age_hours: float, limit: int
    ) -> list[ScoreRecord]: ...


@runtime_checkable
class DecayTimestampProvider(Protocol):
    """Provides per-dimension timestamps for decay calculation."""

    def get_last_enriched_at(self, entity_id: str) -> datetime | None: ...
    def get_last_signal_at(self, entity_id: str) -> datetime | None: ...
    def get_last_graph_sync_at(self, entity_id: str) -> datetime | None: ...


# ── Decay Results ─────────────────────────────────────────────


class DimensionDecayResult:
    """Decay calculation result for a single dimension."""

    __slots__ = (
        "dimension",
        "original_score",
        "decayed_score",
        "decay_factor",
        "days_elapsed",
        "half_life_days",
        "reference_timestamp",
    )

    def __init__(
        self,
        dimension: ScoreDimension,
        original_score: float,
        decayed_score: float,
        decay_factor: float,
        days_elapsed: float,
        half_life_days: float,
        reference_timestamp: datetime | None,
    ):
        self.dimension = dimension
        self.original_score = original_score
        self.decayed_score = decayed_score
        self.decay_factor = decay_factor
        self.days_elapsed = days_elapsed
        self.half_life_days = half_life_days
        self.reference_timestamp = reference_timestamp

    @property
    def decay_pct(self) -> float:
        if self.original_score == 0:
            return 0.0
        return (1.0 - self.decay_factor) * 100.0

    @property
    def is_significantly_decayed(self) -> bool:
        return self.decay_factor < 0.50


class DecayReport:
    """Full decay analysis for an entity."""

    __slots__ = (
        "entity_id",
        "domain",
        "dimension_results",
        "original_composite",
        "decayed_composite",
        "original_tier",
        "decayed_tier",
        "tier_changed",
        "applied_at",
    )

    def __init__(
        self,
        entity_id: str,
        domain: str,
        dimension_results: list[DimensionDecayResult],
        original_composite: float,
        decayed_composite: float,
        original_tier: ScoreTier,
        decayed_tier: ScoreTier,
        applied_at: datetime,
    ):
        self.entity_id = entity_id
        self.domain = domain
        self.dimension_results = dimension_results
        self.original_composite = original_composite
        self.decayed_composite = decayed_composite
        self.original_tier = original_tier
        self.decayed_tier = decayed_tier
        self.tier_changed = original_tier != decayed_tier
        self.applied_at = applied_at

    @property
    def most_decayed_dimension(self) -> DimensionDecayResult | None:
        if not self.dimension_results:
            return None
        return min(self.dimension_results, key=lambda r: r.decay_factor)

    @property
    def composite_decay_pct(self) -> float:
        if self.original_composite == 0:
            return 0.0
        return (
            (self.original_composite - self.decayed_composite) / self.original_composite
        ) * 100.0

    @property
    def needs_re_enrichment(self) -> bool:
        return any(r.is_significantly_decayed for r in self.dimension_results)


# ── Dimension Timestamp Resolution ────────────────────────────


def _resolve_dimension_timestamp(
    dimension: ScoreDimension,
    scored_at: datetime,
    timestamp_provider: DecayTimestampProvider | None,
    entity_id: str,
) -> datetime:
    """Determine the reference timestamp for decay calculation per dimension."""
    if timestamp_provider is None:
        return scored_at

    ts: datetime | None
    if dimension == ScoreDimension.FIT:
        ts = timestamp_provider.get_last_enriched_at(entity_id)
    elif dimension in (
        ScoreDimension.INTENT,
        ScoreDimension.ENGAGEMENT,
        ScoreDimension.READINESS,
    ):
        ts = timestamp_provider.get_last_signal_at(entity_id)
    elif dimension == ScoreDimension.GRAPH_AFFINITY:
        ts = timestamp_provider.get_last_graph_sync_at(entity_id)
    else:
        ts = None

    return ts or scored_at


# ── Decay Engine ──────────────────────────────────────────────


class DecayEngine:
    """
    Applies temporal decay to entity scores.

    Each dimension decays independently based on its configured half-life
    and the time since its reference event (last enrichment for fit,
    last signal for intent/engagement/readiness, last graph sync for
    graph_affinity).
    """

    def __init__(
        self,
        score_store: DecayScoreStore,
        timestamp_provider: DecayTimestampProvider | None = None,
    ):
        self._score_store = score_store
        self._timestamp_provider = timestamp_provider

    def apply_decay(
        self,
        record: ScoreRecord,
        profile: ScoringProfile,
        now: datetime | None = None,
    ) -> DecayReport:
        """Apply decay to all dimensions of a score record."""
        now = now or datetime.now(UTC)
        dimension_results: list[DimensionDecayResult] = []

        for dim, ds in record.dimension_scores.items():
            decay_config = profile.get_decay_config(dim)
            ref_ts = _resolve_dimension_timestamp(
                dim, record.scored_at, self._timestamp_provider, record.entity_id
            )
            days_elapsed = (now - ref_ts).total_seconds() / 86400.0

            factor = compute_decay_factor(
                days_elapsed=days_elapsed,
                half_life_days=decay_config.half_life_days,
                min_score=decay_config.min_score,
                max_decay=decay_config.max_decay,
            )

            decayed = ds.score * factor
            ds.decayed_score = round(decayed, 6)
            ds.decay_factor = round(factor, 6)

            dimension_results.append(
                DimensionDecayResult(
                    dimension=dim,
                    original_score=ds.score,
                    decayed_score=decayed,
                    decay_factor=factor,
                    days_elapsed=round(days_elapsed, 2),
                    half_life_days=decay_config.half_life_days,
                    reference_timestamp=ref_ts,
                )
            )

        decayed_composite, _ = compute_composite(record.dimension_scores, profile, use_decayed=True)
        decayed_composite = apply_gate_penalty(
            decayed_composite, record.missing_fields, profile.gate_penalty
        )

        original_tier = record.tier
        decayed_tier = profile.classify_tier(decayed_composite)

        record.decayed_composite = round(decayed_composite, 6)
        record.decay_applied_at = now
        self._score_store.save_score(record)

        return DecayReport(
            entity_id=record.entity_id,
            domain=record.domain,
            dimension_results=dimension_results,
            original_composite=record.composite_score,
            decayed_composite=decayed_composite,
            original_tier=original_tier,
            decayed_tier=decayed_tier,
            applied_at=now,
        )

    def apply_decay_batch(
        self,
        domain: str,
        profile: ScoringProfile,
        max_age_hours: float = 24.0,
        limit: int = 500,
        now: datetime | None = None,
    ) -> list[DecayReport]:
        """Apply decay to a batch of scores older than max_age_hours since last decay."""
        now = now or datetime.now(UTC)
        records = self._score_store.list_scores_needing_decay(domain, max_age_hours, limit)
        reports: list[DecayReport] = []

        for record in records:
            report = self.apply_decay(record, profile, now)
            reports.append(report)

        return reports

    def preview_decay(
        self,
        record: ScoreRecord,
        profile: ScoringProfile,
        future_days: float = 30.0,
    ) -> list[DecayReport]:
        """Preview decay trajectory at 7-day intervals up to future_days."""
        previews: list[DecayReport] = []
        intervals = max(1, int(future_days / 7))

        for i in range(1, intervals + 1):
            snapshot = deepcopy(record)
            future_now = record.scored_at + timedelta(days=i * 7)

            dimension_results: list[DimensionDecayResult] = []
            for dim, ds in snapshot.dimension_scores.items():
                decay_config = profile.get_decay_config(dim)
                days_elapsed = safe_float(i * 7)
                factor = compute_decay_factor(
                    days_elapsed=days_elapsed,
                    half_life_days=decay_config.half_life_days,
                    min_score=decay_config.min_score,
                    max_decay=decay_config.max_decay,
                )
                decayed = ds.score * factor
                ds.decayed_score = round(decayed, 6)
                ds.decay_factor = round(factor, 6)
                dimension_results.append(
                    DimensionDecayResult(
                        dimension=dim,
                        original_score=ds.score,
                        decayed_score=decayed,
                        decay_factor=factor,
                        days_elapsed=days_elapsed,
                        half_life_days=decay_config.half_life_days,
                        reference_timestamp=record.scored_at,
                    )
                )

            decayed_composite, _ = compute_composite(
                snapshot.dimension_scores, profile, use_decayed=True
            )
            decayed_composite = apply_gate_penalty(
                decayed_composite, record.missing_fields, profile.gate_penalty
            )
            decayed_tier = profile.classify_tier(decayed_composite)

            report = DecayReport(
                entity_id=record.entity_id,
                domain=record.domain,
                dimension_results=dimension_results,
                original_composite=record.composite_score,
                decayed_composite=decayed_composite,
                original_tier=record.tier,
                decayed_tier=decayed_tier,
                applied_at=future_now,
            )
            previews.append(report)

        return previews

    def find_tier_transitions(
        self,
        domain: str,
        profile: ScoringProfile,
        max_age_hours: float = 24.0,
        limit: int = 500,
    ) -> list[DecayReport]:
        """Find entities whose tier would change after decay -- useful for alerting."""
        reports = self.apply_decay_batch(domain, profile, max_age_hours, limit)
        return [r for r in reports if r.tier_changed]
