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

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger("confidence_tracker")

#: Scale of the uncertainty score U. The planner reads U > 2.0 as "targeted"
#: and U > 5.0 as the 5-variation budget.
UNCERTAINTY_SCALE = 10.0


def uncertainty_from_confidences(confidences: Iterable[float | None]) -> float:
    """Uncertainty U of a set of required fields (WU-02 / F-002, owner decision D-9).

    ``U = UNCERTAINTY_SCALE * (1 - mean confidence)``, where a required field
    with no confidence yet (``None``) counts as unknown, i.e. confidence 0.
    Per-pass confidence 0.3 / 0.5 / 0.7 / 0.9 gives U 7 / 5 / 3 / 1. U depends
    only on confidence — never on how many provider variations a pass ran.

    D-9 status: this formula is the candidate pending owner acceptance; it is
    deliberately the only place U is computed so it can be replaced whole.
    """
    values = [0.0 if c is None else min(max(float(c), 0.0), 1.0) for c in confidences]
    if not values:
        return UNCERTAINTY_SCALE
    return UNCERTAINTY_SCALE * (1.0 - sum(values) / len(values))


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

    def begin_pass(self, pass_num: int) -> None:
        """Open pass ``pass_num`` before any field of it is recorded.

        ``current_pass`` must advance even when the pass records nothing (it
        failed, or returned no fields): otherwise had_meaningful_improvement()
        credits the previous pass's gains to it and the loop keeps paying for
        passes that change nothing.
        """
        self.current_pass = max(self.current_pass, pass_num)

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
        """Check whether the latest pass improved confidence meaningfully (F-058).

        Only fields updated on ``current_pass`` count, so a gain made on an
        earlier pass is not credited again. A field first resolved on this pass
        improves from unknown (confidence 0). Sources in ``exclude_sources`` do
        not count, as in :meth:`has_converged`.
        """
        improvements = []
        for state in self._deciding_states():
            if state.pass_number != self.current_pass:
                continue
            prior_confidence = (
                state.prior_attempts[-1]["confidence"] if state.prior_attempts else 0.0
            )
            improvements.append(state.confidence - prior_confidence)

        return bool(improvements) and max(improvements) >= min_delta

    def uncertainty(self, required: Iterable[str] | None = None) -> float:
        """U over the required fields (or every deciding field when none are named)."""
        if required is not None:
            names = list(required)
            return uncertainty_from_confidences(
                self.fields[n].confidence if n in self.fields else None for n in names
            )
        return uncertainty_from_confidences(s.confidence for s in self._deciding_states())

    def _deciding_states(self) -> list[FieldConfidenceState]:
        return [s for s in self.fields.values() if s.source_variation not in self.exclude_sources]

    def get_pass_summary(self) -> dict[str, Any]:
        """Return summary dict for API response."""
        # Counted over the same fields has_converged() decides on, so the summary
        # cannot report converged=True beside a below-threshold count (F-058).
        states = self._deciding_states()
        return {
            "total_passes": self.current_pass,
            "converged": self.has_converged(),
            "fields_above_threshold": sum(1 for s in states if s.confidence >= self.threshold),
            "fields_below_threshold": sum(1 for s in states if s.confidence < self.threshold),
            "average_confidence": (
                sum(s.confidence for s in states) / len(states) if states else 0.0
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
