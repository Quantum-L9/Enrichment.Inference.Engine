"""
Consensus Engine — Weighted multi-variation synthesis.

Synthesizes N variations into single consensus output:
  - Type-aware value selection (mode for strings, median for numbers)
  - Agreement gating: the share of replies carrying the selected value must
    clear the consensus (agreement) threshold
  - Per-field score: a bounded, uncalibrated heuristic (see _field_score).
    Agreement never raises it above the provider's own assertion, duplication
    never raises it, and dissent among distinct values lowers it.

Input: list[dict[str, Any]] — validated responses from N Sonar variations
Output: dict with "fields", "confidence", "per_field_confidence"

Called by: enrichment_orchestrator.py after variation validation
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

import structlog

from app.utils.safe_convert import normalize_confidence, safe_float

logger = structlog.get_logger("consensus")


def apply_field_confidence_thresholds(
    variations: list[dict[str, Any]],
    field_thresholds: Mapping[str, float],
    *,
    requested_fields: Iterable[str] = (),
    default_threshold: float | None = None,
) -> list[dict[str, Any]]:
    """Keep a field only when this reply's confidence clears that field's bar.

    A field's bar is its ``field_thresholds`` entry. A requested field the map
    omits inherits ``default_threshold`` (the field confidence threshold, never
    the agreement threshold). A field that is neither named in the map nor
    requested is unsolicited and dropped, as is any requested field when no
    ``default_threshold`` is given.
    """
    requested = frozenset(requested_fields)
    filtered: list[dict[str, Any]] = []
    for variation in variations:
        confidence = normalize_confidence(variation.get("confidence"))
        kept: dict[str, Any] = {"confidence": variation.get("confidence")}
        for key, value in variation.items():
            if key == "confidence":
                continue
            required = _resolved_threshold(key, field_thresholds, requested, default_threshold)
            if required is None or confidence < required:
                continue
            if value in (None, "", [], {}):
                continue
            kept[key] = value
        filtered.append(kept)
    return filtered


def apply_field_score_thresholds(
    synthesis: dict[str, Any],
    field_thresholds: Mapping[str, float],
    *,
    requested_fields: Iterable[str] = (),
    default_threshold: float | None = None,
) -> dict[str, Any]:
    """Drop a synthesized field whose final score is below that field's bar.

    The per-reply filter above judges each reply's raw confidence; this judges
    the score the response will carry, so agreement or a synthetic value can
    never deliver a field below its consumer bar. Bars resolve exactly as in
    ``apply_field_confidence_thresholds``.
    """
    requested = frozenset(requested_fields)
    fields: dict[str, Any] = {}
    scores: dict[str, float] = {}
    for name, value in synthesis.get("fields", {}).items():
        score = float(synthesis.get("per_field_confidence", {}).get(name, 0.0))
        required = _resolved_threshold(name, field_thresholds, requested, default_threshold)
        if required is None or score < required:
            logger.debug("field_below_field_threshold", field=name, score=score, threshold=required)
            continue
        fields[name] = value
        scores[name] = score
    return {
        **synthesis,
        "fields": fields,
        "per_field_confidence": scores,
        "confidence": sum(scores.values()) / len(scores) if scores else 0.0,
    }


def _resolved_threshold(
    name: str,
    field_thresholds: Mapping[str, float],
    requested: frozenset[str],
    default_threshold: float | None,
) -> float | None:
    """A field's bar: its map entry, else the default when it was requested."""
    required = field_thresholds.get(name)
    if required is None and name in requested:
        return default_threshold
    return required


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
        Replies the agreement ratio is measured over (the caller passes the
        valid replies, so a failed call is never a dissenting vote)

    Returns
    -------
    dict with:
        - fields: dict[str, Any] — consensus values
        - confidence: float — mean of the per-field scores
        - per_field_confidence: dict[str, float] — bounded uncalibrated
          per-field score (``_field_score``), never a calibrated probability
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
        # An empty value is the absence of an answer, not an answer: it neither
        # becomes an enriched field nor counts toward agreement.
        values = [v.get(field) for v in variations if field in v and not _is_empty(v.get(field))]

        if not values:
            continue

        # Agreement among the replies is the admission gate only.
        agreement = _calculate_field_confidence(values, total_attempted)
        if agreement < threshold:
            logger.debug(
                "field_below_threshold",
                field=field,
                confidence=agreement,
                threshold=threshold,
            )
            continue

        winning_value = _select_value(values)
        observations = [
            (v.get(field), normalize_confidence(v.get("confidence")))
            for v in variations
            if field in v and not _is_empty(v.get(field))
        ]
        consensus_fields[field] = winning_value
        per_field_confidence[field] = _field_score(winning_value, observations)

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


def _value_key(value: Any) -> tuple[str, Any]:
    """The one key that decides whether two replies gave the same value.

    Selection, the agreement gate and score support all use it, so the score
    can never borrow confidence from a spelling or type that lost the pick.
    A genuine number compares by numeric value (3 and 3.0 agree); anything
    else by its exact text ("Boston" and "boston", True and "true", differ).
    """
    if _is_number(value):
        return ("number", float(value))
    return ("text", str(value))


def _field_score(selected: Any, observations: list[tuple[Any, float]]) -> float:
    """Bounded, uncalibrated score for the selected value of one field.

    ``observations`` are the (value, normalized provider confidence) pairs of
    the replies that answered this field.

    support: the strongest provider assertion made for exactly the selected
        value. A numeric median no reply stated is supported only as strongly
        as the weaker of the observations it was computed from.
    distinct_share: 1 / (number of distinct values answered). Repeated replies
        of one provider are not independent evidence (no reply carries
        per-field provenance), so identical values count once: duplicating a
        reply cannot raise the score, and dissent lowers it.

    score = min(support, distinct_share). One reply scores its own assertion,
    never the 1/1 self-agreement of 1.0, and a raw 0 stays 0.
    """
    if not observations:
        return 0.0
    selected_key = _value_key(selected)
    supporting = [conf for value, conf in observations if _value_key(value) == selected_key]
    support = max(supporting) if supporting else _median_support(observations)
    distinct = {_value_key(value) for value, _ in observations}
    return min(support, 1.0 / len(distinct))


def _median_support(observations: list[tuple[Any, float]]) -> float:
    """Support for a median no reply stated: the weaker of its middle values."""
    ordered = sorted(observations, key=lambda o: safe_float(o[0]))
    n = len(ordered)
    middle = ordered[n // 2 - 1 : n // 2 + 1] if n % 2 == 0 else [ordered[n // 2]]
    return min(conf for _, conf in middle)


def _calculate_field_confidence(values: list[Any], total_attempted: int) -> float:
    """Agreement ratio for a single field: replies carrying the modal value.

    Logic:
      - 5/5 agree → 1.0
      - 4/5 agree → 0.8
      - 3/5 agree → 0.6
      - All different → 1/5

    This is the admission gate only, not the field's score. The former 0.2
    floor is gone: it let total disagreement pass any threshold at or below
    0.2.
    """
    if not values or total_attempted <= 0:
        return 0.0

    value_counts = Counter(_value_key(v) for v in values)
    max_agreement = max(value_counts.values())
    return max_agreement / total_attempted


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


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _is_number(value: Any) -> bool:
    # bool subclasses int; a boolean is a categorical answer, not a quantity.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _select_value(values: list[Any]) -> Any:
    """
    Select winning value from variations.

    Strategy:
      - Numbers (genuine int/float only): median; an int median stays an int
      - Strings: mode (most common) — "02139" is a string, never a number
      - Booleans: mode
      - Mixed: mode
    """
    if not values:
        return None

    if all(_is_number(v) for v in values):
        median = _median([safe_float(v) for v in values])
        if all(isinstance(v, int) for v in values) and median.is_integer():
            return int(median)
        return median

    # Mode over the shared value key; the first reply carrying the modal key
    # supplies the returned value, so its original type is kept.
    most_common = Counter(_value_key(v) for v in values).most_common(1)[0][0]
    return next(v for v in values if _value_key(v) == most_common)


def _median(numbers: list[float]) -> float:
    """Calculate median of numeric list."""
    sorted_nums = sorted(numbers)
    n = len(sorted_nums)
    if n % 2 == 0:
        return (sorted_nums[n // 2 - 1] + sorted_nums[n // 2]) / 2
    return sorted_nums[n // 2]
