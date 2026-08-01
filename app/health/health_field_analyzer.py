"""
HEALTH field analyzer — deep per-field diagnostics.

Goes beyond the aggregate FieldHealth to provide outlier detection,
distribution analysis, and field-specific recommendations. This powers
the "field health" tab in the dashboard and the CRM field scanner
integration.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any

from app.utils.safe_convert import safe_float

from .health_models import (
    FieldHealth,
    HealthAction,
    RecommendedAction,
)

# ─── Outlier Detection ───────────────────────────────────────


class OutlierResult:
    __slots__ = ("field_name", "entity_id", "value", "z_score", "reason")

    def __init__(self, field_name: str, entity_id: str, value: Any, z_score: float, reason: str):
        self.field_name = field_name
        self.entity_id = entity_id
        self.value = value
        self.z_score = z_score
        self.reason = reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_name": self.field_name,
            "entity_id": self.entity_id,
            "value": self.value,
            "z_score": round(self.z_score, 3),
            "reason": self.reason,
        }


def detect_numeric_outliers(
    field_name: str,
    values: list[tuple[str, float]],
    z_threshold: float = 3.0,
) -> list[OutlierResult]:
    """Z-score outlier detection for numeric fields.

    Args:
        field_name: Name of the field being analyzed.
        values: List of (entity_id, numeric_value) pairs.
        z_threshold: Z-score threshold for outlier classification.

    Returns:
        List of OutlierResult for values exceeding the threshold.
    """
    if len(values) < 5:
        return []

    nums = [v for _, v in values]
    mean = statistics.mean(nums)
    stdev = statistics.stdev(nums) if len(nums) > 1 else 0.0
    if stdev == 0.0:
        return []

    outliers: list[OutlierResult] = []
    for entity_id, val in values:
        z = abs(val - mean) / stdev
        if z >= z_threshold:
            direction = "above" if val > mean else "below"
            outliers.append(
                OutlierResult(
                    field_name=field_name,
                    entity_id=entity_id,
                    value=val,
                    z_score=z,
                    reason=f"Value {val} is {z:.1f}σ {direction} mean ({mean:.2f})",
                )
            )

    return outliers


def detect_categorical_outliers(
    field_name: str,
    values: list[tuple[str, str]],
    min_frequency: float = 0.01,
) -> list[OutlierResult]:
    """Frequency-based outlier detection for categorical fields.

    Values appearing in less than min_frequency fraction of entities
    are flagged as potential outliers (data entry errors, rare categories).
    """
    if len(values) < 10:
        return []

    freq: dict[str, list[str]] = defaultdict(list)
    for entity_id, val in values:
        freq[val.strip().lower()].append(entity_id)

    n = len(values)
    threshold = max(1, int(n * min_frequency))
    outliers: list[OutlierResult] = []

    for val, eids in freq.items():
        if len(eids) <= threshold and len(eids) < n * 0.05:
            for eid in eids:
                outliers.append(
                    OutlierResult(
                        field_name=field_name,
                        entity_id=eid,
                        value=val,
                        z_score=0.0,
                        reason=f"Value '{val}' appears in only {len(eids)}/{n} entities ({len(eids) / n:.1%})",
                    )
                )

    return outliers


# ─── Distribution Analysis ───────────────────────────────────


class DistributionStats:
    __slots__ = (
        "field_name",
        "field_type",
        "count",
        "null_count",
        "fill_rate",
        "mean",
        "median",
        "stdev",
        "min_val",
        "max_val",
        "p25",
        "p75",
        "unique_count",
        "top_values",
        "entropy",
    )

    def __init__(self, field_name: str, field_type: str):
        self.field_name = field_name
        self.field_type = field_type
        self.count = 0
        self.null_count = 0
        self.fill_rate = 0.0
        self.mean: float | None = None
        self.median: float | None = None
        self.stdev: float | None = None
        self.min_val: float | None = None
        self.max_val: float | None = None
        self.p25: float | None = None
        self.p75: float | None = None
        self.unique_count = 0
        self.top_values: dict[str, int] = {}
        self.entropy: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "field_name": self.field_name,
            "field_type": self.field_type,
            "count": self.count,
            "null_count": self.null_count,
            "fill_rate": round(self.fill_rate, 4),
            "unique_count": self.unique_count,
        }
        if self.mean is not None:
            d["mean"] = round(self.mean, 4)
            d["median"] = round(self.median, 4) if self.median is not None else None
            d["stdev"] = round(self.stdev, 4) if self.stdev is not None else None
            d["min"] = self.min_val
            d["max"] = self.max_val
            d["p25"] = round(self.p25, 4) if self.p25 is not None else None
            d["p75"] = round(self.p75, 4) if self.p75 is not None else None
        if self.top_values:
            d["top_values"] = self.top_values
        if self.entropy is not None:
            d["entropy"] = round(self.entropy, 4)
        return d


def analyze_distribution(
    field_name: str,
    values: list[Any],
    total_entities: int,
) -> DistributionStats:
    """Compute distribution statistics for a field across all entities."""
    non_null = [v for v in values if v is not None]

    is_numeric = all(isinstance(v, (int, float)) for v in non_null[:100]) and non_null
    field_type = "numeric" if is_numeric else "categorical"
    stats = DistributionStats(field_name, field_type)
    stats.count = total_entities
    stats.null_count = total_entities - len(non_null)
    stats.fill_rate = len(non_null) / total_entities if total_entities > 0 else 0.0

    if not non_null:
        return stats

    if is_numeric:
        nums = [safe_float(v) for v in non_null]
        stats.mean = statistics.mean(nums)
        stats.median = statistics.median(nums)
        stats.stdev = statistics.stdev(nums) if len(nums) > 1 else 0.0
        stats.min_val = min(nums)
        stats.max_val = max(nums)
        sorted_nums = sorted(nums)
        n = len(sorted_nums)
        stats.p25 = sorted_nums[n // 4] if n >= 4 else sorted_nums[0]
        stats.p75 = sorted_nums[(3 * n) // 4] if n >= 4 else sorted_nums[-1]
        stats.unique_count = len(set(nums))
    else:
        str_vals = [str(v).strip().lower() for v in non_null]
        freq: dict[str, int] = defaultdict(int)
        for sv in str_vals:
            freq[sv] += 1
        stats.unique_count = len(freq)
        stats.top_values = dict(sorted(freq.items(), key=lambda x: x[1], reverse=True)[:10])

        n = len(str_vals)
        if n > 0:
            probs = [c / n for c in freq.values()]
            stats.entropy = -sum(p * math.log2(p) for p in probs if p > 0)

    return stats


# ─── Field Health Recommendations ─────────────────────────────


def recommend_field_actions(
    field_health: FieldHealth,
    distribution: DistributionStats,
    outlier_count: int = 0,
) -> list[RecommendedAction]:
    """Generate field-specific health improvement recommendations."""
    actions: list[RecommendedAction] = []
    fname = field_health.field_name

    if field_health.fill_rate < 0.50:
        priority = 1.0 if field_health.is_gate_critical else 0.80
        actions.append(
            RecommendedAction(
                action=HealthAction.FILL_MISSING,
                field_name=fname,
                reason=f"Fill rate {field_health.fill_rate:.0%} — "
                f"{'gate-critical, blocks matching' if field_health.is_gate_critical else 'below 50% threshold'}",
                priority=priority,
            )
        )

    if field_health.avg_confidence < 0.50 and field_health.fill_rate > 0.30:
        actions.append(
            RecommendedAction(
                action=HealthAction.VERIFY_FIELD,
                field_name=fname,
                reason=f"Avg confidence {field_health.avg_confidence:.2f} — values present but unreliable",
                priority=0.75,
            )
        )

    if field_health.staleness_p50_days > 60:
        actions.append(
            RecommendedAction(
                action=HealthAction.REFRESH_STALE,
                field_name=fname,
                reason=f"Median staleness {field_health.staleness_p50_days:.0f} days — data likely outdated",
                priority=0.65,
            )
        )

    if outlier_count > 0:
        actions.append(
            RecommendedAction(
                action=HealthAction.FLAG_OUTLIER,
                field_name=fname,
                reason=f"{outlier_count} outlier values detected — may indicate data quality issues",
                priority=0.55,
            )
        )

    if distribution.field_type == "categorical" and distribution.entropy is not None:
        if distribution.unique_count <= 2 and distribution.fill_rate > 0.80:
            pass
        elif distribution.entropy < 0.5 and distribution.unique_count > 5:
            actions.append(
                RecommendedAction(
                    action=HealthAction.VERIFY_FIELD,
                    field_name=fname,
                    reason=f"Low entropy ({distribution.entropy:.2f}) with {distribution.unique_count} categories — "
                    f"possible over-concentration or default values",
                    priority=0.45,
                )
            )

    actions.sort(key=lambda a: a.priority, reverse=True)
    return actions


# ─── Full Field Diagnostic ────────────────────────────────────


class FieldDiagnostic:
    __slots__ = ("field_health", "distribution", "outliers", "recommendations")

    def __init__(
        self,
        field_health: FieldHealth,
        distribution: DistributionStats,
        outliers: list[OutlierResult],
        recommendations: list[RecommendedAction],
    ):
        self.field_health = field_health
        self.distribution = distribution
        self.outliers = outliers
        self.recommendations = recommendations

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_name": self.field_health.field_name,
            "health_score": round(self.field_health.health_score, 4),
            "is_gate_critical": self.field_health.is_gate_critical,
            "is_scoring_dimension": self.field_health.is_scoring_dimension,
            "distribution": self.distribution.to_dict(),
            "outlier_count": len(self.outliers),
            "outliers": [o.to_dict() for o in self.outliers[:20]],
            "recommendations": [
                {
                    "action": r.action.value,
                    "field": r.field_name,
                    "reason": r.reason,
                    "priority": r.priority,
                }
                for r in self.recommendations
            ],
        }


def run_field_diagnostic(
    field_health: FieldHealth,
    entity_values: list[tuple[str, Any]],
    total_entities: int,
) -> FieldDiagnostic:
    """Run full diagnostic on a single field: distribution + outliers + recommendations."""
    all_values = [v for _, v in entity_values]
    dist = analyze_distribution(field_health.field_name, all_values, total_entities)

    outliers: list[OutlierResult] = []
    non_null = [(eid, v) for eid, v in entity_values if v is not None]
    if dist.field_type == "numeric":
        numeric_pairs = [(eid, safe_float(v)) for eid, v in non_null if isinstance(v, (int, float))]
        outliers = detect_numeric_outliers(field_health.field_name, numeric_pairs)
    else:
        str_pairs = [(eid, str(v)) for eid, v in non_null]
        outliers = detect_categorical_outliers(field_health.field_name, str_pairs)

    recommendations = recommend_field_actions(field_health, dist, len(outliers))

    return FieldDiagnostic(
        field_health=field_health,
        distribution=dist,
        outliers=outliers,
        recommendations=recommendations,
    )
