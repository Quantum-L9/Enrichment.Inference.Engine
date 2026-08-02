"""TASK-041: compile FieldConfidenceMap into FeatureEvidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.models.feature_evidence import (
    FeatureEvidence,
    ValueState,
    compile_field_confidence_map,
    feature_evidence_from_field_confidence,
)
from app.models.field_confidence import FieldConfidence, FieldConfidenceMap, FieldSource

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "feature_evidence"


def test_compile_map_aligns_with_field_confidence() -> None:
    cmap = FieldConfidenceMap(
        fields={
            "polymer_type": FieldConfidence(
                field_name="polymer_type",
                value="HDPE",
                confidence=0.95,
                source=FieldSource.CRM,
            ),
            "available_lbs": FieldConfidence(
                field_name="available_lbs",
                value=120000,
                confidence=0.82,
                source=FieldSource.INFERENCE,
                kb_fragment_ids=["capacity-rule-2.1.0"],
            ),
        }
    )
    items = compile_field_confidence_map(
        cmap,
        subject_ref="res.partner:102",
        execution_version="eie-0.9.0",
        source_ref="eie.convergence:run-123",
        feature_id_by_field={
            "polymer_type": "material.polymer_type",
            "available_lbs": "facility.capacity.available_lbs_month",
        },
        unit_by_feature={"facility.capacity.available_lbs_month": "lb/month"},
    )
    assert len(items) == 2
    by_id = {e.feature_id: e for e in items}
    assert by_id["material.polymer_type"].value_state == ValueState.OBSERVED
    inferred = by_id["facility.capacity.available_lbs_month"]
    assert inferred.value_state == ValueState.INFERRED
    assert inferred.provenance.rule_or_model_version == "capacity-rule-2.1.0"
    assert inferred.unit == "lb/month"
    assert isinstance(inferred.value.value, int)


def test_inferred_without_version_or_kb_rejected() -> None:
    field = FieldConfidence(
        field_name="capacity",
        value=1,
        confidence=0.5,
        source=FieldSource.INFERENCE,
    )
    with pytest.raises(ValidationError, match="rule_or_model_version"):
        feature_evidence_from_field_confidence(
            field=field,
            subject_ref="res.partner:1",
            feature_id="facility.capacity.available_lbs_month",
            execution_version="eie-0.9.0",
            source_ref="eie.convergence:run-1",
        )


def test_batch_example_validates() -> None:
    payload = json.loads((CONTRACT / "examples" / "feature-evidence-batch.json").read_text())
    assert len(payload) == 2
    for item in payload:
        FeatureEvidence.model_validate(item)


def test_odoo_projection_fixture_covers_registry() -> None:
    registry = yaml.safe_load((CONTRACT / "feature_registry.yaml").read_text())
    fixture = yaml.safe_load((CONTRACT / "fixtures" / "odoo_projection.yaml").read_text())
    reg_ids = {f["feature_id"] for f in registry["features"]}
    proj_ids = {p["feature_id"] for p in fixture["projections"]}
    assert reg_ids == proj_ids
    assert all(p["write_mode"] == "proposal_only" for p in fixture["projections"])


def test_schema_allof_inferred_requires_version() -> None:
    schema = yaml.safe_load((CONTRACT / "feature-evidence.schema.yaml").read_text())
    assert schema.get("allOf"), "compiled schema must encode inferred→version rule"
