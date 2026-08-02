"""TASK-029: FeatureEvidence → Odoo mapping contract."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.models.evidence_odoo_mapping import (
    ApplyDecision,
    EvidenceApplyInput,
    EvidenceOdooMappingContract,
    OdooFieldState,
    decide_apply,
    load_evidence_odoo_mapping,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DIR = ROOT / "contracts" / "evidence_odoo_mapping"
CONTRACT = CONTRACT_DIR / "evidence-odoo-mapping.yaml"
NEGATIVES = CONTRACT_DIR / "negative_examples"


def test_contract_loads() -> None:
    contract = load_evidence_odoo_mapping(CONTRACT)
    assert contract.policy.merge_strategy == "merge_not_overwrite"
    assert contract.policy.review_mode.value == "always"
    assert contract.policy.default_write_mode == "proposal_only"
    assert contract.policy.human_review_precedence is True
    assert all(p.write_mode.value == "proposal_only" for p in contract.projections)


def test_negative_auto_write_rejected() -> None:
    data = yaml.safe_load((NEGATIVES / "auto-write-forbidden.yaml").read_text())
    with pytest.raises(ValidationError):
        EvidenceOdooMappingContract.model_validate(data)


def test_negative_bad_feature_id_rejected() -> None:
    data = yaml.safe_load((NEGATIVES / "unknown-feature.yaml").read_text())
    with pytest.raises(ValidationError):
        EvidenceOdooMappingContract.model_validate(data)


def test_unlisted_feature_rejected() -> None:
    contract = load_evidence_odoo_mapping(CONTRACT)
    decision = decide_apply(
        contract,
        EvidenceApplyInput(
            feature_id="facility.unknown.metric",
            value_kind="number",
            value_state="observed",
        ),
        OdooFieldState(empty=True),
    )
    assert decision == ApplyDecision.REJECT_UNLISTED


def test_review_mode_always_proposes() -> None:
    contract = load_evidence_odoo_mapping(CONTRACT)
    decision = decide_apply(
        contract,
        EvidenceApplyInput(
            feature_id="material.polymer_type",
            value_kind="string",
            value_state="observed",
            observed_at=datetime(2026, 8, 2, tzinfo=UTC),
        ),
        OdooFieldState(empty=True),
    )
    assert decision == ApplyDecision.PROPOSE_REVIEW


def test_human_approved_blocks_overwrite() -> None:
    contract = load_evidence_odoo_mapping(CONTRACT)
    decision = decide_apply(
        contract,
        EvidenceApplyInput(
            feature_id="material.polymer_type",
            value_kind="string",
            value_state="inferred",
            observed_at=datetime(2026, 8, 2, tzinfo=UTC),
        ),
        OdooFieldState(empty=False, human_approved=True, value="HDPE"),
    )
    assert decision == ApplyDecision.REJECT_HUMAN_PRECEDENCE


def test_stale_evidence_rejected_when_field_newer() -> None:
    contract = load_evidence_odoo_mapping(CONTRACT)
    decision = decide_apply(
        contract,
        EvidenceApplyInput(
            feature_id="material.contamination_pct",
            value_kind="number",
            value_state="observed",
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        ),
        OdooFieldState(
            empty=False,
            value=1.2,
            revised_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
    )
    assert decision == ApplyDecision.REJECT_STALE
