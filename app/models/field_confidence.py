"""Per-field confidence tracking for the enrichment-inference convergence loop.

The existing consensus engine returns **one float per entity**. Every downstream
consumer—uncertainty engine, convergence controller, pass telemetry, HEALTH,
SCORE—needs **one float per field** so the system knows *which* fields to
target on the next pass.

Exports:
    FieldSource         – Enum: where a field value originated.
    FieldConfidence     – Single-field confidence record with full provenance.
    FieldConfidenceMap  – Dict wrapper with aggregate queries (weakest, coverage, …).
    compute_field_confidences – Builds a FieldConfidenceMap from raw consensus payloads.
"""

from __future__ import annotations

from app.utils.safe_convert import safe_float
import statistics
from collections import defaultdict
from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FieldSource(StrEnum):
    """Origin of a field value inside the convergence loop."""

    CRM = "crm"
    ENRICHMENT = "enrichment"
    INFERENCE = "inference"
    MANUAL = "manual"
    SEED = "seed"


# ---------------------------------------------------------------------------
# Core model
# ---------------------------------------------------------------------------


class FieldConfidence(BaseModel):
    """Confidence record for a single field on a single entity.

    Attributes:
        field_name:           API name of the field (e.g. ``contamination_tolerance_pct``).
        value:                The resolved value after consensus / inference.
        confidence:           0.0–1.0 agreement-weighted score for this field.
        source:               How the value was produced.
        variation_agreement:  Fraction of variations that returned this value
                              (e.g. 4/5 → 0.80).  ``None`` for non-enrichment sources.
        pass_discovered:      Loop pass number where this field first appeared (1-indexed).
        kb_fragment_ids:      KB atoms/rules that contributed to this field's value.
    """

    field_name: str
    value: Any = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source: FieldSource = FieldSource.ENRICHMENT
    variation_agreement: float | None = Field(default=None, ge=0.0, le=1.0)
    pass_discovered: int = Field(default=1, ge=1)
    kb_fragment_ids: list[str] = Field(default_factory=list)

    @field_validator("confidence", mode="before")
    @classmethod
    def clamp_confidence(cls, v: Any) -> float:
        return max(0.0, min(1.0, safe_float(v)))


# ---------------------------------------------------------------------------
# Aggregate wrapper
# ---------------------------------------------------------------------------


class FieldConfidenceMap(BaseModel):
    """Keyed collection of :class:`FieldConfidence` records with aggregate helpers.

    The map is the canonical per-field confidence payload embedded in every
    ``PassResult`` and ``ConvergeResponse``.  Downstream services deserialise
    this directly from the TransportPacket payload.
    """

    fields: dict[str, FieldConfidence] = Field(default_factory=dict)

    # -- Mutators -----------------------------------------------------------

    def set(self, fc: FieldConfidence) -> None:
        """Insert or replace a field confidence record."""
        self.fields[fc.field_name] = fc

    def merge(self, other: FieldConfidenceMap) -> None:
        """Merge *other* into self.  Higher-confidence wins per field."""
        for name, fc in other.fields.items():
            existing = self.fields.get(name)
            if existing is None or fc.confidence > existing.confidence:
                self.fields[name] = fc

    # -- Queries ------------------------------------------------------------

    def get(self, field_name: str) -> FieldConfidence | None:
        return self.fields.get(field_name)

    def weakest_fields(self, n: int = 5) -> list[FieldConfidence]:
        """Return the *n* fields with the lowest confidence, sorted ascending."""
        return sorted(self.fields.values(), key=lambda f: f.confidence)[:n]

    def fields_below_threshold(self, threshold: float = 0.65) -> list[FieldConfidence]:
        """All fields whose confidence is strictly below *threshold*."""
        return [f for f in self.fields.values() if f.confidence < threshold]

    def fields_above_threshold(self, threshold: float = 0.65) -> list[FieldConfidence]:
        return [f for f in self.fields.values() if f.confidence >= threshold]

    def avg_confidence(self) -> float:
        if not self.fields:
            return 0.0
        return statistics.mean(f.confidence for f in self.fields.values())

    def coverage_ratio(self, total_expected: int) -> float:
        """Fraction of *total_expected* fields that have any value."""
        if total_expected <= 0:
            return 0.0
        filled = sum(1 for f in self.fields.values() if f.value is not None)
        return filled / total_expected

    def field_names(self) -> list[str]:
        return list(self.fields.keys())

    def confident_fields(self, threshold: float = 0.65) -> dict[str, Any]:
        """Return ``{field_name: value}`` for fields meeting *threshold*."""
        return {name: fc.value for name, fc in self.fields.items() if fc.confidence >= threshold}

    def source_breakdown(self) -> dict[FieldSource, int]:
        """Count of fields per :class:`FieldSource`."""
        counts: dict[FieldSource, int] = defaultdict(int)
        for fc in self.fields.values():
            counts[fc.source] += 1
        return dict(counts)

    # -- Serialisation ------------------------------------------------------

    def to_flat_dict(self) -> dict[str, dict[str, Any]]:
        """TransportPacket-friendly serialisation (no Pydantic wrapping)."""
        return {name: fc.model_dump(mode="json") for name, fc in self.fields.items()}

    @classmethod
    def from_flat_dict(cls, data: dict[str, dict[str, Any]]) -> FieldConfidenceMap:
        return cls(fields={k: FieldConfidence(**v) for k, v in data.items()})

    def __len__(self) -> int:
        return len(self.fields)

    def __contains__(self, field_name: str) -> bool:
        return field_name in self.fields

    def __iter__(self):
        return iter(self.fields.values())


# ---------------------------------------------------------------------------
# Builder: consensus payloads → per-field confidence
# ---------------------------------------------------------------------------


def compute_field_confidences(
    validated_payloads: Sequence[dict[str, Any]],
    target_schema: dict[str, str] | None = None,
    *,
    total_attempted: int = 0,
    pass_number: int = 1,
    kb_fragment_ids: list[str] | None = None,
) -> FieldConfidenceMap:
    """Compute per-field confidence from validated consensus payloads.

    For each field present in any payload:
    1. Count how many payloads contain it (*agreement*).
    2. Average the per-payload ``confidence`` values that included it.
    3. Penalise if ``total_attempted`` > ``len(validated_payloads)``
       (some variations failed / were rejected).
    4. Combine:  ``field_confidence = agreement × avg_conf × penalty``.

    Args:
        validated_payloads:  List of dicts that passed validation.  Each dict
                             has ``"confidence"`` plus enriched field key/values.
        target_schema:       If provided, used to identify expected fields.
        total_attempted:     Total variation count fired (including failures).
        pass_number:         Current convergence loop pass (1-indexed).
        kb_fragment_ids:     KB atoms/rules injected for this enrichment call.

    Returns:
        A :class:`FieldConfidenceMap` with one entry per observed field.
    """
    if not validated_payloads:
        return FieldConfidenceMap()

    total_valid = len(validated_payloads)
    total_attempted = max(total_attempted, total_valid)
    penalty = total_valid / total_attempted if total_attempted > 0 else 1.0
    kb_ids = kb_fragment_ids or []

    # Collect per-field observations
    field_values: dict[str, list[Any]] = defaultdict(list)
    field_confs: dict[str, list[float]] = defaultdict(list)

    reserved = {"confidence", "tokens_used", "processing_time_ms"}

    for payload in validated_payloads:
        payload_conf = safe_float(payload.get("confidence", 0.0))
        for key, val in payload.items():
            if key in reserved:
                continue
            field_values[key].append(val)
            field_confs[key].append(payload_conf)

    # Build map
    fcm = FieldConfidenceMap()
    for field_name in field_values:
        values = field_values[field_name]
        confs = field_confs[field_name]
        agreement = len(values) / total_valid

        # Value consensus: pick the most common value
        value_counts: dict[str, int] = defaultdict(int)
        for v in values:
            value_counts[_hashable(v)] += 1
        winner_key = max(value_counts, key=value_counts.get)  # type: ignore[arg-type]
        # Recover the original (unhashable-safe) value
        winner_value = next(v for v in values if _hashable(v) == winner_key)
        value_agreement = value_counts[winner_key] / len(values)

        avg_conf = statistics.mean(confs) if confs else 0.0
        combined = round(agreement * avg_conf * penalty * value_agreement, 4)

        fcm.set(
            FieldConfidence(
                field_name=field_name,
                value=winner_value,
                confidence=combined,
                source=FieldSource.ENRICHMENT,
                variation_agreement=round(value_agreement, 4),
                pass_discovered=pass_number,
                kb_fragment_ids=kb_ids,
            )
        )

    return fcm


def _hashable(v: Any) -> str:
    """Produce a stable string key for any JSON-serialisable value."""
    if isinstance(v, (list, dict)):
        import json

        return json.dumps(v, sort_keys=True, default=str)
    return str(v)
