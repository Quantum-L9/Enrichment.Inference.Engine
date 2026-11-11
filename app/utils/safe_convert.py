"""Safe numeric conversions for untrusted or loosely typed inputs."""

from __future__ import annotations

from math import isfinite
from typing import Any


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert ``value`` to float, returning ``default`` on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_confidence(value: object) -> float:
    """Effective confidence of one reported confidence value, in [0, 1].

    The single normalization rule for a provider's self-reported confidence,
    shared by response validation, consensus filtering and scoring, the
    early-stop check and the D-9 v2 tracker. That confidence is an
    uncalibrated claim, not a probability.

    Absent, boolean, non-numeric and non-finite values are unusable and count
    as 0, so malformed input can never create confidence. Finite values,
    including numeric strings, are clamped to [0, 1].
    """
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not isfinite(number):
        return 0.0
    return max(0.0, min(1.0, number))
