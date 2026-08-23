"""Tests for app/engines/inference/grade_engine.py

Covers: HDPE premium/standard/recycled, PET bottle/fiber, unknown polymer,
        missing-field confidence drop, confidence from input match score.

Product API: classify(entity_fields, grade_defs, threshold=...) -> GradeResult | None
"""

from __future__ import annotations

from typing import Any

from app.engines.inference.grade_engine import (
    GradeDefinition,
    GradeResult,
    classify,
    load_grade_definitions,
)

_GRADE_KB: dict[str, Any] = {
    "material_grades": [
        {
            "grade_id": "hdpe_premium",
            "grade_label": "Premium HDPE",
            "quality_tier": "premium",
            "application_class": "film",
            "conditions": [
                {"field": "polymer_type", "value": "HDPE"},
                {"field": "contamination_pct", "min": 0.0, "max": 2.0},
                {"field": "mfi_range", "value": "0.5-3.0"},
            ],
        },
        {
            "grade_id": "hdpe_standard",
            "grade_label": "Standard HDPE",
            "quality_tier": "standard",
            "application_class": "injection",
            "conditions": [
                {"field": "polymer_type", "value": "HDPE"},
                {"field": "contamination_pct", "min": 2.5, "max": 5.0},
                {"field": "mfi_range", "value": "0.5-3.0"},
            ],
        },
        {
            "grade_id": "hdpe_recycled",
            "grade_label": "Recycled HDPE",
            "quality_tier": "recycled",
            "application_class": "mixed",
            "conditions": [
                {"field": "polymer_type", "value": "HDPE"},
                {"field": "contamination_pct", "min": 5.5, "max": 20.0},
            ],
        },
        {
            "grade_id": "pet_bottle",
            "grade_label": "PET Bottle",
            "quality_tier": "food_contact",
            "application_class": "bottle",
            "conditions": [
                {"field": "polymer_type", "value": "PET"},
                {"field": "application", "value": "bottle"},
            ],
        },
        {
            "grade_id": "pet_fiber",
            "grade_label": "PET Fiber",
            "quality_tier": "industrial",
            "application_class": "fiber",
            "conditions": [
                {"field": "polymer_type", "value": "PET"},
                {"field": "application", "value": "fiber"},
            ],
        },
    ]
}


def _defs() -> list[GradeDefinition]:
    return load_grade_definitions(_GRADE_KB)


class TestGradeEngine:
    """Tests for material grading inference against GradeDefinition lists."""

    def test_grade_hdpe_premium(self) -> None:
        result = classify(
            {
                "polymer_type": "HDPE",
                "contamination_pct": 1.0,
                "mfi_range": "0.5-3.0",
            },
            _defs(),
            threshold=0.75,
        )
        assert result is not None
        assert result.grade_label == "Premium HDPE"
        assert result.confidence >= 0.85

    def test_grade_hdpe_standard(self) -> None:
        result = classify(
            {
                "polymer_type": "HDPE",
                "contamination_pct": 3.5,
                "mfi_range": "0.5-3.0",
            },
            _defs(),
            threshold=0.75,
        )
        assert result is not None
        assert result.grade_label == "Standard HDPE"
        assert result.confidence >= 0.75

    def test_grade_hdpe_recycled(self) -> None:
        result = classify(
            {
                "polymer_type": "HDPE",
                "contamination_pct": 7.0,
            },
            _defs(),
            threshold=0.75,
        )
        assert result is not None
        assert result.grade_label == "Recycled HDPE"

    def test_grade_pet_bottle(self) -> None:
        result = classify(
            {"polymer_type": "PET", "application": "bottle"},
            _defs(),
            threshold=0.75,
        )
        assert result is not None
        assert "bottle" in result.grade_label.lower() or "PET" in result.grade_label

    def test_grade_pet_fiber(self) -> None:
        result = classify(
            {"polymer_type": "PET", "application": "fiber"},
            _defs(),
            threshold=0.75,
        )
        assert result is not None
        assert "fiber" in result.grade_label.lower() or "PET" in result.grade_label

    def test_grade_unknown_polymer(self) -> None:
        result = classify({"polymer_type": "UNKNOWN_POLYMER"}, _defs(), threshold=0.75)
        # No conditions match → live classify returns None (not a fabricated grade).
        assert result is None or (isinstance(result, GradeResult) and result.confidence < 0.7)

    def test_grade_confidence_from_input_confidence(self) -> None:
        full = classify(
            {
                "polymer_type": "HDPE",
                "contamination_pct": 1.0,
                "mfi_range": "0.5-3.0",
            },
            _defs(),
            threshold=0.75,
        )
        weaker = classify(
            {
                "polymer_type": "HDPE",
                "contamination_pct": 1.5,
            },
            _defs(),
            threshold=0.75,
        )
        assert full is not None
        assert weaker is not None
        # Confidence is the input-field match score (or fallback * 0.7), not a kwarg.
        assert full.confidence == full.match_score
        assert weaker.confidence < full.confidence

    def test_missing_critical_field_lowers_confidence(self) -> None:
        result_full = classify(
            {
                "polymer_type": "HDPE",
                "contamination_pct": 1.5,
                "mfi_range": "0.5-3.0",
            },
            _defs(),
            threshold=0.75,
        )
        result_partial = classify(
            {"polymer_type": "HDPE"},
            _defs(),
            threshold=0.75,
        )
        assert result_full is not None
        assert result_partial is not None
        assert result_partial.confidence < result_full.confidence
