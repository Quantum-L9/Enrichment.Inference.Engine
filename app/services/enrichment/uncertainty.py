"""
Uncertainty engine for enrichment confidence management.

Applies confidence thresholds, generates uncertainty flags, and optionally
filters low-confidence fields from enrichment results.

L9 Architecture Note:
    This module is chassis-agnostic. It never imports FastAPI.
    It is called by WaterfallEngine after consensus synthesis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_LOW_THRESHOLD = 0.5
DEFAULT_HIGH_THRESHOLD = 0.85
DEFAULT_CRITICAL_THRESHOLD = 0.3


@dataclass
class UncertaintyResult:
    """Result of uncertainty analysis on enrichment data."""

    fields: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    flags: list[str] = field(default_factory=list)
    filtered_fields: list[str] = field(default_factory=list)
    risk_level: str = "low"


@dataclass
class UncertaintyConfig:
    """Configuration for uncertainty thresholds."""

    low_threshold: float = DEFAULT_LOW_THRESHOLD
    high_threshold: float = DEFAULT_HIGH_THRESHOLD
    critical_threshold: float = DEFAULT_CRITICAL_THRESHOLD
    filter_below_critical: bool = False
    flag_field_level: bool = True


def apply_uncertainty(
    fields: dict[str, Any],
    confidence: float,
    field_confidences: dict[str, float] | None = None,
    config: UncertaintyConfig | None = None,
) -> UncertaintyResult:
    """
    Apply uncertainty policy to enrichment results.

    Analyzes confidence levels and generates appropriate flags and risk levels.
    Optionally filters fields below critical threshold.

    Args:
        fields: Enriched field dict
        confidence: Overall confidence score (0.0-1.0)
        field_confidences: Optional per-field confidence scores
        config: Uncertainty configuration (uses defaults if None)

    Returns:
        UncertaintyResult with:
            - fields: Potentially filtered fields
            - confidence: Adjusted confidence (unchanged unless critical)
            - flags: List of uncertainty flags
            - filtered_fields: Names of fields that were filtered
            - risk_level: "low", "medium", "high", or "critical"
    """
    config = config if config is not None else UncertaintyConfig()

    flags: list[str] = []
    filtered: list[str] = []
    result_fields = dict(fields)

    risk_level = _determine_risk_level(confidence, config)

    if confidence < config.critical_threshold:
        flags.append("critical_low_confidence")
        flags.append("manual_review_required")
    elif confidence < config.low_threshold:
        flags.append("low_confidence")
        flags.append("needs_review")
    elif confidence < config.high_threshold:
        flags.append("moderate_confidence")

    if field_confidences and config.flag_field_level:
        for field_name, field_conf in field_confidences.items():
            if field_conf < config.critical_threshold:
                flags.append(f"field_critical:{field_name}")
                if config.filter_below_critical and field_name in result_fields:
                    del result_fields[field_name]
                    filtered.append(field_name)
            elif field_conf < config.low_threshold:
                flags.append(f"field_uncertain:{field_name}")

    if config.filter_below_critical and confidence < config.critical_threshold:
        logger.warning(
            "uncertainty_critical_confidence",
            confidence=confidence,
            fields_count=len(fields),
        )

    logger.info(
        "uncertainty_applied",
        confidence=round(confidence, 3),
        risk_level=risk_level,
        flags_count=len(flags),
        filtered_count=len(filtered),
    )

    return UncertaintyResult(
        fields=result_fields,
        confidence=confidence,
        flags=flags,
        filtered_fields=filtered,
        risk_level=risk_level,
    )


def _determine_risk_level(confidence: float, config: UncertaintyConfig) -> str:
    """Determine risk level based on confidence thresholds."""
    if confidence < config.critical_threshold:
        return "critical"
    if confidence < config.low_threshold:
        return "high"
    if confidence < config.high_threshold:
        return "medium"
    return "low"


def should_proceed(
    result: UncertaintyResult,
    require_confidence: float = 0.5,
    block_on_critical: bool = True,
) -> bool:
    """
    Determine if enrichment should proceed based on uncertainty analysis.

    Args:
        result: UncertaintyResult from apply_uncertainty()
        require_confidence: Minimum confidence to proceed
        block_on_critical: Whether to block on critical risk level

    Returns:
        True if enrichment should proceed, False if it should be blocked
    """
    if block_on_critical and result.risk_level == "critical":
        return False

    return result.confidence >= require_confidence


def aggregate_uncertainties(
    results: list[UncertaintyResult],
) -> UncertaintyResult:
    """
    Aggregate multiple uncertainty results into a single summary.

    Useful when combining results from multiple enrichment sources.

    Args:
        results: List of UncertaintyResult objects

    Returns:
        Aggregated UncertaintyResult with:
            - fields: Merged from all results (later results override)
            - confidence: Average confidence
            - flags: Union of all flags
            - risk_level: Worst (highest) risk level
    """
    if not results:
        return UncertaintyResult()

    merged_fields: dict[str, Any] = {}
    all_flags: set[str] = set()
    all_filtered: set[str] = set()
    confidences: list[float] = []

    risk_priority = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    worst_risk = "low"

    for result in results:
        merged_fields.update(result.fields)
        all_flags.update(result.flags)
        all_filtered.update(result.filtered_fields)
        confidences.append(result.confidence)

        if risk_priority.get(result.risk_level, 0) > risk_priority.get(worst_risk, 0):
            worst_risk = result.risk_level

    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    return UncertaintyResult(
        fields=merged_fields,
        confidence=round(avg_confidence, 3),
        flags=sorted(all_flags),
        filtered_fields=sorted(all_filtered),
        risk_level=worst_risk,
    )
