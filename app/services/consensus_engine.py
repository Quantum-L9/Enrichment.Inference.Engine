"""
Consensus Engine — Weighted multi-variation synthesis.

Synthesizes N variations into single consensus output:
  - Agreement-based weighting (5/5 agree = high confidence)
  - Per-field confidence scoring (NEW)
  - Consensus threshold gating
  - Type-aware value selection (mode for strings, median for numbers)

Input: list[dict[str, Any]] — validated responses from N Sonar variations
Output: dict with "fields", "confidence", "per_field_confidence"

Called by: enrichment_orchestrator.py after variation validation
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import structlog

logger = structlog.get_logger("consensus")


def synthesize(
    variations: list[dict[str, Any]],
    threshold: float = 0.6,
    total_attempted: int = 5,
) -> dict[str, Any]:
    """
    Synthesize consensus from multiple enrichment variations.

    Parameters
    ----------
    variations : list[dict]
        Validated responses from N Sonar API calls
    threshold : float
        Minimum agreement ratio to include field (default 0.6)
    total_attempted : int
        Total variations attempted (for confidence calculation)

    Returns
    -------
    dict with:
        - fields: dict[str, Any] — consensus values
        - confidence: float — aggregate confidence (0.0-1.0)
        - per_field_confidence: dict[str, float] — confidence per field (NEW)
    """
    if not variations:
        return {
            "fields": {},
            "confidence": 0.0,
            "per_field_confidence": {},
        }

    all_fields = _get_all_fields(variations)
    consensus_fields: dict[str, Any] = {}
    per_field_confidence: dict[str, float] = {}

    for field in all_fields:
        values = [v.get(field) for v in variations if field in v]

        if not values:
            continue

        # Calculate field-level agreement
        field_confidence = _calculate_field_confidence(values, total_attempted)

        # Apply threshold gate
        if field_confidence < threshold:
            logger.debug(
                "field_below_threshold",
                field=field,
                confidence=field_confidence,
                threshold=threshold,
            )
            continue

        # Select winning value
        winning_value = _select_value(values)
        consensus_fields[field] = winning_value
        per_field_confidence[field] = field_confidence

    # Calculate aggregate confidence
    aggregate_confidence = (
        sum(per_field_confidence.values()) / len(per_field_confidence)
        if per_field_confidence
        else 0.0
    )

    logger.info(
        "consensus_synthesized",
        fields=len(consensus_fields),
        variations=len(variations),
        aggregate_confidence=round(aggregate_confidence, 3),
    )

    return {
        "fields": consensus_fields,
        "confidence": aggregate_confidence,
        "per_field_confidence": per_field_confidence,
    }


def _calculate_field_confidence(values: list[Any], total_attempted: int) -> float:
    """
    Calculate confidence score for a single field based on variation agreement.

    Logic:
      - 5/5 agree → 1.0
      - 4/5 agree → 0.8
      - 3/5 agree → 0.6
      - 2/5 agree → 0.4
      - All different → 0.2 (floor)
    """
    if not values:
        return 0.0

    # Normalize values for comparison
    value_counts: dict[str, int] = {}
    for v in values:
        v_normalized = str(v).strip().lower()
        value_counts[v_normalized] = value_counts.get(v_normalized, 0) + 1

    max_agreement = max(value_counts.values())

    # Agreement ratio
    confidence = max_agreement / total_attempted

    # Floor at 0.2 (some data is better than none)
    return max(0.2, confidence)


# Keys `validate_response` attaches to every normalized variation that are
# response metadata, not enriched domain fields. `confidence` in particular is
# guaranteed present on each variation, so without this exclusion it wins
# consensus on every pass and reaches consumers as an enriched field named
# "confidence" (observed on the live Odoo -> Gate -> EIE rail).
_RESPONSE_META_KEYS: frozenset[str] = frozenset({"confidence"})


def _get_all_fields(variations: list[dict[str, Any]]) -> set[str]:
    """Extract unique enriched field names across all variations.

    Response metadata keys are excluded: they describe the variation, they are
    not values the caller asked to enrich.
    """
    fields: set[str] = set()
    for v in variations:
        fields.update(k for k in v if k not in _RESPONSE_META_KEYS)
    return fields


def _select_value(values: list[Any]) -> Any:
    """
    Select winning value from variations.

    Strategy:
      - Strings: mode (most common)
      - Numbers: median
      - Booleans: mode
      - Mixed: mode
    """
    if not values:
        return None

    # Try numeric median
    try:
        numeric_values = [float(v) for v in values]
        return _median(numeric_values)
    except (ValueError, TypeError):
        pass

    # Fall back to mode (most common)
    counter = Counter(str(v) for v in values)
    most_common = counter.most_common(1)[0][0]

    # Return original type if possible
    for v in values:
        if str(v) == most_common:
            return v

    return most_common


def _median(numbers: list[float]) -> float:
    """Calculate median of numeric list."""
    sorted_nums = sorted(numbers)
    n = len(sorted_nums)
    if n % 2 == 0:
        return (sorted_nums[n // 2 - 1] + sorted_nums[n // 2]) / 2
    return sorted_nums[n // 2]
