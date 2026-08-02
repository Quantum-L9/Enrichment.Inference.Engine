"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: enrichment
layer: [models]
tags: [feature-evidence, provenance, contract]
owner: engine-team
status: active
--- /L9_META ---

FeatureEvidence contract (TASK-019).

Defines attributed, versioned evidence payloads emitted by EIE.
Extends FieldConfidence concepts without replacing the convergence loop.
TASK-041 compiles FieldConfidenceMap batches into FeatureEvidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.field_confidence import FieldConfidence, FieldConfidenceMap, FieldSource

ENTITY_REF_PATTERN = r"^[a-z0-9_.-]+:[^\s]+$"
FEATURE_ID_PATTERN = r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)+$"


class ValueState(StrEnum):
    OBSERVED = "observed"
    REPORTED = "reported"
    INFERRED = "inferred"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"
    STALE = "stale"
    CONFLICTING = "conflicting"
    SUPERSEDED = "superseded"


class EvidenceSource(StrEnum):
    CRM = "crm"
    ENRICHMENT = "enrichment"
    INFERENCE = "inference"
    MANUAL = "manual"
    SEED = "seed"
    GRAPH = "graph"
    EXTERNAL = "external"


class ProvenanceSourceType(StrEnum):
    ODOO = "odoo"
    ENRICHMENT = "enrichment"
    INFERENCE = "inference"
    MANUAL = "manual"
    SEED = "seed"
    GRAPH = "graph"
    EXTERNAL = "external"


class TypedValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["string", "number", "boolean", "string_list", "null"]
    value: Any

    @model_validator(mode="after")
    def value_matches_kind(self) -> TypedValue:
        kind = self.kind
        value = self.value
        if kind == "null" and value is not None:
            raise ValueError("null typed value must be null")
        if kind == "string" and not isinstance(value, str):
            raise ValueError("string typed value requires str")
        if kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("number typed value requires int|float")
        if kind == "boolean" and not isinstance(value, bool):
            raise ValueError("boolean typed value requires bool")
        if kind == "string_list" and (
            not isinstance(value, list) or not all(isinstance(x, str) for x in value)
        ):
            raise ValueError("string_list typed value requires list[str]")
        return self


class EvidenceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: ProvenanceSourceType
    source_ref: str = Field(min_length=1)
    method: str = Field(min_length=1)
    actor_ref: str | None = None
    rule_or_model_version: str | None = None
    content_hash: str | None = None


class FeatureEvidence(BaseModel):
    """Attributed, versioned feature evidence payload."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1.0.0-draft"] = "1.0.0-draft"
    domain: Literal["plasticos"] = "plasticos"
    feature_id: str = Field(pattern=FEATURE_ID_PATTERN)
    subject_ref: str = Field(pattern=ENTITY_REF_PATTERN)
    value: TypedValue
    unit: str | None = None
    value_state: ValueState
    confidence: float = Field(ge=0.0, le=1.0)
    source: EvidenceSource
    method: str = Field(min_length=1)
    observed_at: datetime
    valid_until: datetime | None = None
    provenance: EvidenceProvenance
    evidence_ref: str | None = Field(default=None, pattern=ENTITY_REF_PATTERN)
    supersedes_ref: str | None = None
    conflicts_with_refs: list[str] = Field(default_factory=list)
    execution_version: str = Field(min_length=1)

    @field_validator("conflicts_with_refs")
    @classmethod
    def unique_conflicts(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("conflicts_with_refs must be unique")
        return values

    @model_validator(mode="after")
    def inferred_requires_version(self) -> FeatureEvidence:
        if self.value_state == ValueState.INFERRED and not self.provenance.rule_or_model_version:
            raise ValueError("inferred evidence requires provenance.rule_or_model_version")
        return self


def _typed_value_from_raw(raw: Any) -> TypedValue:
    if raw is None:
        return TypedValue(kind="null", value=None)
    if isinstance(raw, bool):
        return TypedValue(kind="boolean", value=raw)
    if isinstance(raw, int):
        return TypedValue(kind="number", value=raw)
    if isinstance(raw, float):
        return TypedValue(kind="number", value=raw)
    if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        return TypedValue(kind="string_list", value=list(raw))
    return TypedValue(kind="string", value=str(raw))


def _source_from_field(source: FieldSource) -> EvidenceSource:
    mapping = {
        FieldSource.CRM: EvidenceSource.CRM,
        FieldSource.ENRICHMENT: EvidenceSource.ENRICHMENT,
        FieldSource.INFERENCE: EvidenceSource.INFERENCE,
        FieldSource.MANUAL: EvidenceSource.MANUAL,
        FieldSource.SEED: EvidenceSource.SEED,
    }
    return mapping.get(source, EvidenceSource.ENRICHMENT)


def feature_evidence_from_field_confidence(
    *,
    field: FieldConfidence,
    subject_ref: str,
    feature_id: str | None = None,
    value_state: ValueState | None = None,
    observed_at: datetime | None = None,
    execution_version: str,
    source_ref: str,
    rule_or_model_version: str | None = None,
    unit: str | None = None,
    evidence_ref: str | None = None,
) -> FeatureEvidence:
    """Adapter: FieldConfidence → FeatureEvidence (versioned/attributed)."""

    fid = feature_id or field.field_name.replace("-", "_")
    if "." not in fid:
        fid = f"field.{fid}"
    state = value_state
    if state is None:
        state = (
            ValueState.INFERRED if field.source == FieldSource.INFERENCE else ValueState.OBSERVED
        )
    method = "field_confidence_adapter"
    if field.kb_fragment_ids:
        method = ",".join(field.kb_fragment_ids[:3])
    prov_source = {
        EvidenceSource.CRM: ProvenanceSourceType.ODOO,
        EvidenceSource.ENRICHMENT: ProvenanceSourceType.ENRICHMENT,
        EvidenceSource.INFERENCE: ProvenanceSourceType.INFERENCE,
        EvidenceSource.MANUAL: ProvenanceSourceType.MANUAL,
        EvidenceSource.SEED: ProvenanceSourceType.SEED,
    }[_source_from_field(field.source)]
    version = rule_or_model_version
    if state == ValueState.INFERRED and not version and field.kb_fragment_ids:
        version = field.kb_fragment_ids[0]
    return FeatureEvidence(
        feature_id=fid,
        subject_ref=subject_ref,
        value=_typed_value_from_raw(field.value),
        unit=unit,
        value_state=state,
        confidence=field.confidence,
        source=_source_from_field(field.source),
        method=method,
        observed_at=observed_at or datetime.now(UTC),
        provenance=EvidenceProvenance(
            source_type=prov_source,
            source_ref=source_ref,
            method=method,
            rule_or_model_version=version,
        ),
        evidence_ref=evidence_ref,
        execution_version=execution_version,
    )


def compile_field_confidence_map(
    confidence_map: FieldConfidenceMap,
    *,
    subject_ref: str,
    execution_version: str,
    source_ref: str,
    feature_id_by_field: dict[str, str] | None = None,
    unit_by_feature: dict[str, str | None] | None = None,
    observed_at: datetime | None = None,
) -> list[FeatureEvidence]:
    """Compile a live FieldConfidenceMap into FeatureEvidence payloads.

    Aligns TASK-041 acceptance: every emitted item is attributed; inferred
    items carry rule/model version (explicit or from kb_fragment_ids).
    """
    mapping = feature_id_by_field or {}
    units = unit_by_feature or {}
    stamp = observed_at or datetime.now(UTC)
    out: list[FeatureEvidence] = []
    for name, field in confidence_map.fields.items():
        fid = mapping.get(name)
        unit = units.get(fid) if fid else units.get(name)
        out.append(
            feature_evidence_from_field_confidence(
                field=field,
                subject_ref=subject_ref,
                feature_id=fid,
                execution_version=execution_version,
                source_ref=source_ref,
                unit=unit,
                observed_at=stamp,
            )
        )
    return out
