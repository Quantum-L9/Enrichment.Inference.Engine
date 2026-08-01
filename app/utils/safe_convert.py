"""Safe numeric conversions for untrusted or loosely typed inputs."""

from __future__ import annotations

from typing import Any


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert ``value`` to float, returning ``default`` on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
