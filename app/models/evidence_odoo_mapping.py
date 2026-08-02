"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: enrichment
layer: [models]
tags: [evidence, odoo, mapping, merge-not-overwrite]
owner: engine-team
status: active
--- /L9_META ---

FeatureEvidence → Odoo mapping contract loader and apply policy (TASK-029).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

FEATURE_ID_PATTERN = r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)+$"
CONTRACT_REL = Path("contracts/evidence_odoo_mapping/evidence-odoo-mapping.yaml")


class WriteMode(StrEnum):
    PROPOSAL_ONLY = "proposal_only"


class MergeStrategy(StrEnum):
    MERGE_NOT_OVERWRITE = "merge_not_overwrite"


class ReviewMode(StrEnum):
    ALWAYS = "always"
    INFERRED_ONLY = "inferred_only"


class ApplyDecision(StrEnum):
    PROPOSE_REVIEW = "propose_review"
    REJECT_UNLISTED = "reject_unlisted"
    REJECT_KIND_MISMATCH = "reject_kind_mismatch"
    REJECT_STALE = "reject_stale"
    REJECT_HUMAN_PRECEDENCE = "reject_human_precedence"
    REJECT_OVERWRITE = "reject_overwrite"


class MappingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merge_strategy: Literal["merge_not_overwrite"]
    review_mode: ReviewMode
    default_write_mode: Literal["proposal_only"]
    human_review_precedence: Literal[True] = True


class MappingProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature_id: str = Field(pattern=FEATURE_ID_PATTERN)
    odoo_model: str = Field(min_length=1)
    odoo_field: str = Field(min_length=1)
    unit: str | None = None
    write_mode: WriteMode
    value_kinds: list[Literal["string", "number", "boolean", "string_list", "null"]] = Field(
        min_length=1
    )


class EvidenceOdooMappingContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1.0.0-draft"]
    domain: Literal["plasticos"]
    owner: Literal["eie"]
    description: str | None = None
    policy: MappingPolicy
    projections: list[MappingProjection] = Field(min_length=1)
    rules: list[str] = Field(default_factory=list)

    @field_validator("projections")
    @classmethod
    def unique_feature_ids(cls, values: list[MappingProjection]) -> list[MappingProjection]:
        ids = [p.feature_id for p in values]
        if len(ids) != len(set(ids)):
            raise ValueError("projection feature_id values must be unique")
        return values

    def projection_for(self, feature_id: str) -> MappingProjection | None:
        for projection in self.projections:
            if projection.feature_id == feature_id:
                return projection
        return None


class OdooFieldState(BaseModel):
    """Consumer-side field observation used to evaluate merge-not-overwrite."""

    model_config = ConfigDict(extra="forbid")

    value: Any = None
    empty: bool = True
    human_approved: bool = False
    revised_at: datetime | None = None


class EvidenceApplyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature_id: str = Field(pattern=FEATURE_ID_PATTERN)
    value_kind: Literal["string", "number", "boolean", "string_list", "null"]
    value_state: str
    observed_at: datetime | None = None


def load_evidence_odoo_mapping(path: Path | None = None) -> EvidenceOdooMappingContract:
    """Load and validate the producer mapping contract from YAML."""
    contract_path = path or (Path.cwd() / CONTRACT_REL)
    if not contract_path.is_file():
        # Resolve relative to repo root when cwd is nested
        root = Path(__file__).resolve().parents[2]
        contract_path = root / CONTRACT_REL
    data = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return EvidenceOdooMappingContract.model_validate(data)


def decide_apply(
    contract: EvidenceOdooMappingContract,
    evidence: EvidenceApplyInput,
    field_state: OdooFieldState,
) -> ApplyDecision:
    """Evaluate merge-not-overwrite + review policy for one evidence item.

    With ``proposal_only`` + ``review_mode: always``, successful allowlisted
    evidence yields ``propose_review`` — never a direct write.
    """
    projection = contract.projection_for(evidence.feature_id)
    if projection is None:
        return ApplyDecision.REJECT_UNLISTED
    if evidence.value_kind not in projection.value_kinds:
        return ApplyDecision.REJECT_KIND_MISMATCH
    if contract.policy.human_review_precedence and field_state.human_approved:
        return ApplyDecision.REJECT_HUMAN_PRECEDENCE
    if (
        contract.policy.merge_strategy == MergeStrategy.MERGE_NOT_OVERWRITE
        and not field_state.empty
        and field_state.revised_at is not None
        and evidence.observed_at is not None
        and evidence.observed_at < field_state.revised_at
    ):
        return ApplyDecision.REJECT_STALE
    if (
        contract.policy.merge_strategy == MergeStrategy.MERGE_NOT_OVERWRITE
        and not field_state.empty
        and not field_state.human_approved
        and evidence.value_state in {"stale", "superseded"}
    ):
        return ApplyDecision.REJECT_OVERWRITE
    # proposal_only + review_mode always → review proposal only
    if projection.write_mode != WriteMode.PROPOSAL_ONLY:
        return ApplyDecision.REJECT_OVERWRITE
    if contract.policy.review_mode == ReviewMode.ALWAYS:
        return ApplyDecision.PROPOSE_REVIEW
    if contract.policy.review_mode == ReviewMode.INFERRED_ONLY:
        if evidence.value_state == "inferred":
            return ApplyDecision.PROPOSE_REVIEW
        if field_state.empty:
            return ApplyDecision.PROPOSE_REVIEW
        return ApplyDecision.REJECT_OVERWRITE
    return ApplyDecision.PROPOSE_REVIEW
