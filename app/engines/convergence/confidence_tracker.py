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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from math import fsum
from typing import Any

import structlog

# Re-exported: the planner and controller import it from here.
from app.utils.safe_convert import normalize_confidence as normalize_confidence

logger = structlog.get_logger("confidence_tracker")

#: Scale of the uncertainty scores. The planner reads U_action > 2.0 as
#: "targeted" and U_action > 5.0 as the 5-variation budget.
UNCERTAINTY_SCALE = 10.0

#: Local identifier of the uncertainty policy implemented below (issue #266).
UNCERTAINTY_POLICY = "D9-v2"

# D-9 v2 (operator decision 2026-11-11, issue #266) supersedes D-9 v1, the
# average-only policy, in its entirety. Two scores are computed over ONE
# population of effective confidences c*:
#
#   U_mean   = 10 * (1 - mean(c*))   diagnostic and historical: the public
#                                    EnrichResponse.uncertainty_score and the
#                                    per-pass inferences[*].uncertainty
#   U_action = 10 * (1 - min(c*))    operational: drives the planner's mode and
#                                    pass-level variation budget
#
# An empty population is cold start: both scores are 10. Neither score decides
# readiness. A field is ready only when its value is present and its effective
# confidence meets its own threshold (the request's field_thresholds entry, else
# the loop's confidence threshold); no aggregate overrides that.
#
# D-9 v1's acceptance claim (code and test docstrings said "owner-accepted
# 2026-10-29" while merged PR #259 said "pending owner acceptance") was never
# independently verified. D-9 v2 is a new decision, not a confirmation of v1.


def value_present(value: Any) -> bool:
    """A field value counts as present unless it is None or an empty string.

    Legitimate falsy values such as ``0`` and ``False`` are present.
    """
    return value is not None and value != ""


def uncertainty_from_confidences(confidences: Iterable[object]) -> float:
    """U_mean: ``10 * (1 - mean effective confidence)``; 10 for no fields.

    Diagnostic only under D-9 v2. Per-pass confidence 0.3 / 0.5 / 0.7 / 0.9
    gives 7 / 5 / 3 / 1. Variation count is never an input.
    """
    values = [normalize_confidence(c) for c in confidences]
    if not values:
        return UNCERTAINTY_SCALE
    return UNCERTAINTY_SCALE * (1.0 - fsum(values) / len(values))


def action_uncertainty_from_confidences(confidences: Iterable[object]) -> float:
    """U_action: ``10 * (1 - min effective confidence)``; 10 for no fields.

    The weakest required field decides: one poorly supported or missing field
    cannot be concealed by confident peers, and raising any field's confidence
    can never raise the score.
    """
    values = [normalize_confidence(c) for c in confidences]
    if not values:
        return UNCERTAINTY_SCALE
    return UNCERTAINTY_SCALE * (1.0 - min(values))


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
        required_fields: Sequence[str] | None = None,
        field_thresholds: Mapping[str, float] | None = None,
    ):
        self.fields: dict[str, FieldConfidenceState] = {}
        self.threshold = confidence_threshold
        # Sources whose fields never decide convergence (see has_converged).
        self.exclude_sources = exclude_sources
        # The caller's explicit schema, when it named one. Readiness, the summary
        # and both uncertainty scores are then measured over exactly these
        # fields, whatever source last set them.
        self.required_fields: list[str] | None = list(required_fields) if required_fields else None
        # Consumer per-field bars; a missing entry falls back to `threshold`.
        self.field_thresholds: dict[str, float] = dict(field_thresholds or {})
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
        """Update or create field confidence state.

        Confidence is normalized here, where it enters the tracker, so every
        later reading (scores, readiness, improvement, the serialized summary)
        sees a finite value in [0, 1] and malformed input can never raise.
        """
        confidence = normalize_confidence(confidence)
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
        """Return tracked fields that are not ready at their own threshold."""
        return [name for name in self.fields if not self.field_ready(name)]

    def threshold_for(self, name: str) -> float:
        """The readiness bar for one field: its consumer threshold, else the loop's."""
        return self.field_thresholds.get(name, self.threshold)

    def effective_confidence(self, name: str) -> float:
        """c* of one field: 0 when untracked, valueless or carrying unusable confidence."""
        state = self.fields.get(name)
        if state is None or not value_present(state.current_value):
            return 0.0
        return normalize_confidence(state.confidence)

    def field_ready(self, name: str) -> bool:
        """present(value) and c* >= the field's own threshold."""
        state = self.fields.get(name)
        if state is None or not value_present(state.current_value):
            return False
        return normalize_confidence(state.confidence) >= self.threshold_for(name)

    def deciding_fields(self) -> list[str]:
        """The fields readiness and both uncertainty scores are measured over.

        The explicit schema when one was named; otherwise every tracked field
        except those whose latest value came from an excluded source.
        """
        if self.required_fields is not None:
            return list(self.required_fields)
        return [s.field_name for s in self._deciding_states()]

    def has_converged(self) -> bool:
        """Check whether every deciding field is ready.

        With an explicit schema every requested field must be present and meet
        its own threshold, whatever source set it. Without one, fields whose
        latest value came from a source in `exclude_sources` are not considered;
        if no field remains, the tracker has not converged. The same rule backs
        the serialized `converged` flag and the loop's stop decision, so the two
        cannot disagree.
        """
        names = self.deciding_fields()
        if not names:
            return False
        return all(self.field_ready(name) for name in names)

    def had_meaningful_improvement(self, min_delta: float) -> bool:
        """Check whether the latest pass improved confidence meaningfully (F-058).

        Measured over the fields that decide readiness (:meth:`deciding_fields`:
        the explicit schema when one was named), in effective confidence c*. An
        optional field's gain therefore never buys another pass while every
        required field is stuck, and a field that goes from a missing value to
        a present one improves from 0 even if its reported confidence did not
        move. Only fields updated on ``current_pass`` count, so a gain made on
        an earlier pass is not credited again; a field first resolved on this
        pass improves from unknown (0).
        """
        improvements = []
        for name in self.deciding_fields():
            state = self.fields.get(name)
            if state is None or state.pass_number != self.current_pass:
                continue
            prior = state.prior_attempts[-1] if state.prior_attempts else None
            prior_effective = (
                normalize_confidence(prior["confidence"])
                if prior is not None and value_present(prior["value"])
                else 0.0
            )
            improvements.append(self.effective_confidence(name) - prior_effective)

        return bool(improvements) and max(improvements) >= min_delta

    def _population(self, required: Iterable[str] | None) -> list[float]:
        """Effective confidences of one selected population, shared by both scores."""
        names = list(required) if required is not None else self.deciding_fields()
        return [self.effective_confidence(name) for name in names]

    def uncertainty(self, required: Iterable[str] | None = None) -> float:
        """U_mean over the required fields (or the deciding fields when none are named)."""
        return uncertainty_from_confidences(self._population(required))

    def action_uncertainty(self, required: Iterable[str] | None = None) -> float:
        """U_action over the same population as :meth:`uncertainty`."""
        return action_uncertainty_from_confidences(self._population(required))

    def _deciding_states(self) -> list[FieldConfidenceState]:
        return [s for s in self.fields.values() if s.source_variation not in self.exclude_sources]

    def get_pass_summary(self) -> dict[str, Any]:
        """Return summary dict for API response."""
        # Counted over the same fields, with the same per-field bars, that
        # has_converged() decides on, so the summary cannot report
        # converged=True beside a below-threshold count (F-058).
        names = self.deciding_fields()
        ready = sum(1 for name in names if self.field_ready(name))
        return {
            "total_passes": self.current_pass,
            "converged": self.has_converged(),
            "fields_above_threshold": ready,
            "fields_below_threshold": len(names) - ready,
            "average_confidence": (
                fsum(self.effective_confidence(name) for name in names) / len(names)
                if names
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
