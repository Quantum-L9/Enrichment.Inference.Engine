"""FeatureEvidence contract tests (TASK-019)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.models.feature_evidence import (
    EvidenceSource,
    FeatureEvidence,
    ProvenanceSourceType,
    TypedValue,
    ValueState,
    feature_evidence_from_field_confidence,
)
from app.models.field_confidence import FieldConfidence, FieldSource

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "feature_evidence"


def _load(name: str) -> dict:
    return json.loads((CONTRACT / name).read_text())


def test_positive_example_validates() -> None:
    FeatureEvidence.model_validate(_load("examples/feature-evidence.json"))


def test_inferred_without_version_rejected() -> None:
    payload = _load("negative_examples/inferred-without-version.json")
    with pytest.raises(ValidationError, match="rule_or_model_version"):
        FeatureEvidence.model_validate(payload)


def test_feature_registry_ids_are_canonical() -> None:
    registry = yaml.safe_load((CONTRACT / "feature_registry.yaml").read_text())
    assert registry["owner"] == "eie"
    ids = [f["feature_id"] for f in registry["features"]]
    assert len(ids) >= 1
    for fid in ids:
        assert fid[0].islower()
        assert "." in fid


def test_adapter_from_field_confidence_inferred_requires_version() -> None:
    fc = FieldConfidence(
        field_name="contamination_pct",
        value=2.5,
        confidence=0.8,
        source=FieldSource.INFERENCE,
    )
    with pytest.raises(ValidationError):
        feature_evidence_from_field_confidence(
            field=fc,
            subject_ref="res.partner:1",
            feature_id="material.contamination_pct",
            execution_version="eie-0.9.0",
            source_ref="eie.convergence:run-1",
            rule_or_model_version=None,
        )


def test_adapter_observed_ok() -> None:
    fc = FieldConfidence(
        field_name="polymer_type",
        value="HDPE",
        confidence=0.95,
        source=FieldSource.CRM,
    )
    ev = feature_evidence_from_field_confidence(
        field=fc,
        subject_ref="res.partner:102",
        feature_id="material.polymer_type",
        execution_version="eie-0.9.0",
        source_ref="odoo.partner:102",
        value_state=ValueState.OBSERVED,
    )
    assert ev.source == EvidenceSource.CRM
    assert ev.provenance.source_type == ProvenanceSourceType.ODOO
    assert ev.execution_version == "eie-0.9.0"
    assert ev.value == TypedValue(kind="string", value="HDPE")


def test_schema_file_forbids_transport_fields() -> None:
    schema = yaml.safe_load((CONTRACT / "feature-evidence.schema.yaml").read_text())
    props = schema.get("properties") or {}
    for banned in ("packet_uuid", "tenant_uuid", "correlation_id", "transport_hash"):
        assert banned not in props
