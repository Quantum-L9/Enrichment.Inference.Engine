"""
Confidence Tracker — Per-field confidence state across convergence passes.

Tracks:
  - Current confidence per field
  - Prior attempt history
  - Convergence status
  - Low-confidence field identification

Consumed by: convergence_controller.py for pass targeting
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger("confidence_tracker")


@dataclass
class FieldConfidenceState:
    """Tracks enrichment confidence for a single field across passes."""

    field_name: str
    current_value: Any
    confidence: float
    pass_number: int
    source_variation: str
    prior_attempts: list[dict[str, Any]] = field(default_factory=list)


class ConfidenceTracker:
    """
    Tracks per-field confidence across enrichment passes.
    Determines when convergence criteria are met.
    """

    def __init__(
        self,
        confidence_threshold: float = 0.85,
        exclude_sources: frozenset[str] = frozenset(),
    ):
        self.fields: dict[str, FieldConfidenceState] = {}
        self.threshold = confidence_threshold
        # Sources whose fields never decide convergence (see has_converged).
        self.exclude_sources = exclude_sources
        self.current_pass = 0

    def update_field(
        self,
        name: str,
        value: Any,
        confidence: float,
        pass_num: int,
        source: str,
    ) -> None:
        """Update or create field confidence state."""
        if name in self.fields:
            prior = self.fields[name]
            prior.prior_attempts.append(
                {
                    "pass_num": prior.pass_number,
                    "value": prior.current_value,
                    "confidence": prior.confidence,
                }
            )

        self.fields[name] = FieldConfidenceState(
            field_name=name,
            current_value=value,
            confidence=confidence,
            pass_number=pass_num,
            source_variation=source,
            prior_attempts=(self.fields[name].prior_attempts if name in self.fields else []),
        )
        self.current_pass = max(self.current_pass, pass_num)

    def get_low_confidence_fields(self) -> list[str]:
        """Return fields below confidence threshold."""
        return [name for name, state in self.fields.items() if state.confidence < self.threshold]

    def has_converged(self) -> bool:
        """Check if all fields meet threshold.

        Fields whose latest value came from a source in `exclude_sources` are
        not considered; if no field remains, the tracker has not converged.
        The same rule backs the serialized `converged` flag and the loop's stop
        decision, so the two cannot disagree.
        """
        states = [s for s in self.fields.values() if s.source_variation not in self.exclude_sources]
        if not states:
            return False
        return all(state.confidence >= self.threshold for state in states)

    def had_meaningful_improvement(self, min_delta: float) -> bool:
        """Check if last pass improved confidence meaningfully."""
        improvements = []
        for state in self.fields.values():
            if state.prior_attempts:
                prior_confidence = state.prior_attempts[-1]["confidence"]
                delta = state.confidence - prior_confidence
                improvements.append(delta)

        return bool(improvements) and max(improvements) >= min_delta

    def get_pass_summary(self) -> dict[str, Any]:
        """Return summary dict for API response."""
        return {
            "total_passes": self.current_pass,
            "converged": self.has_converged(),
            "fields_above_threshold": sum(
                1 for s in self.fields.values() if s.confidence >= self.threshold
            ),
            "fields_below_threshold": len(self.get_low_confidence_fields()),
            "average_confidence": (
                sum(s.confidence for s in self.fields.values()) / len(self.fields)
                if self.fields
                else 0.0
            ),
        }

    def get_field_confidence(self, field_name: str) -> float:
        """Get confidence for a specific field."""
        if field_name not in self.fields:
            return 0.0
        return self.fields[field_name].confidence

    def get_all_confidences(self) -> dict[str, float]:
        """Return all field confidence scores as a dict."""
        return {name: state.confidence for name, state in self.fields.items()}

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for response payload."""
        return {
            "threshold": self.threshold,
            "current_pass": self.current_pass,
            "converged": self.has_converged(),
            "fields": {
                name: {
                    "confidence": state.confidence,
                    "value": state.current_value,
                    "pass_number": state.pass_number,
                    "attempts": len(state.prior_attempts) + 1,
                }
                for name, state in self.fields.items()
            },
            "summary": self.get_pass_summary(),
        }
