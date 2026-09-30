"""
Adaptive variation budget controller.

Decides how many Sonar calls to fire per entity based on:
  1. Target field completeness (how many fields are we missing?)
  2. Entity richness (how much context do we have to research with?)
  3. Prior confidence (if re-enriching, was last confidence low?)

Output: integer 2–max_variations.
Higher = more API cost, better consensus quality.
Lower = cheaper, sufficient for well-known entities.

Audit fixes:
  - M8: Inspects ALL entity fields, not just 3 hardcoded ones.
  - H3: Returns 2 minimum (not 3), respects engine output.
"""

from __future__ import annotations

from typing import Any


def compute_uncertainty(
    entity: dict[str, Any],
    target_schema: dict[str, str] | None = None,
    last_confidence: float = 0.5,
    max_variations: int = 5,
) -> int:
    """
    Compute variation budget for this entity.

    Returns integer in range [2, max_variations].
    """
    # ── Missing target fields ────────────────────────
    missing = 0
    total_target = 0
    if target_schema:
        for field_name in target_schema:
            total_target += 1
            val = entity.get(field_name)
            if val is None or val == "" or val == []:
                missing += 1

    completeness = (1 - (missing / total_target)) if total_target > 0 else 0.5

    # ── Entity richness ──────────────────────────────
    # Count ALL non-empty fields on the entity
    filled = sum(1 for v in entity.values() if v is not None and v != "" and v != [] and v != {})
    # Normalize: 10+ fields = fully rich
    richness = min(1.0, filled / 10.0)

    # ── Prior confidence decay ───────────────────────
    conf_factor = 1.0 - last_confidence

    # ── Combined uncertainty (0 = easy, 1 = hard) ────
    score = (1 - completeness) * 0.4 + conf_factor * 0.3 + (1 - richness) * 0.3

    # Floor at 2, but never above the caller's max_variations. A request
    # for one variation must not grow a second call whose timeout is then
    # counted as disagreement and drops the field the first call returned.
    variations = max(2, min(max_variations, round(score * max_variations) + 2))
    return min(variations, max(1, max_variations))
