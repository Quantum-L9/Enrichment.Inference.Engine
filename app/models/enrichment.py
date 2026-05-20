"""
Enrichment model types.

Re-exports from convergence_controller and schemas for backward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Re-export from schemas
from .schemas import EnrichRequest, EnrichResponse


@dataclass
class ConvergenceState:
    """Accumulated state across all passes."""

    known_fields: dict[str, Any] = field(default_factory=dict)
    confidence_map: dict[str, float] = field(default_factory=dict)
    inferred_fields: dict[str, Any] = field(default_factory=dict)
    pass_results: list[Any] = field(default_factory=list)
    total_tokens: int = 0
    converged: bool = False
    convergence_reason: str = ""
    unlock_map: dict[str, float] = field(default_factory=dict)
    uncertainty_score: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class InferenceResult:
    """Result from inference rule execution."""

    field_name: str
    value: Any
    confidence: float = 0.0
    rule_id: str = ""
    source_fields: list[str] = field(default_factory=list)


@dataclass
class FieldResult:
    """Result for a single field from enrichment."""

    field_name: str
    value: Any
    confidence: float = 0.0
    source: str = ""
    inferred: bool = False


__all__ = [
    "ConvergenceState",
    "EnrichRequest",
    "EnrichResponse",
    "FieldResult",
    "InferenceResult",
]
