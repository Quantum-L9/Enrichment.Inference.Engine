"""Tests for app/services/validation_engine.py — EXPANDED

Extends existing test coverage with schema coercion and field-level
validation rules.

Source: ~200 lines | Target coverage: 90%
"""

from __future__ import annotations

import pytest

from app.services.validation_engine import _coerce, validate_response

# Alias for test compatibility
validate_payload = validate_response
coerce_value = _coerce


# ---------------------------------------------------------------------------
# Schema Coercion
# ---------------------------------------------------------------------------


class TestSchemaCoercion:
    """Expand schema coercion tests."""

    def test_coerce_string_to_integer(self):
        result = coerce_value("42", int)
        assert result == 42

    def test_coerce_string_to_float(self):
        result = coerce_value("3.14", float)
        assert abs(result - 3.14) < 0.001

    def test_coerce_integer_to_string(self):
        result = coerce_value(42, str)
        assert result == "42"

    def test_coerce_list_to_string(self):
        result = coerce_value(["A", "B", "C"], str)
        assert isinstance(result, str)
        # Should produce comma-separated or JSON
        assert "A" in result and "B" in result

    def test_coerce_boolean_strings(self):
        assert coerce_value("true", bool) is True
        assert coerce_value("false", bool) is False
        assert coerce_value("True", bool) is True

    def test_invalid_coercion_returns_none(self):
        with pytest.raises((ValueError, TypeError)):
            coerce_value("not_a_number", int)
        result = validate_payload({"confidence": 0.8, "n": "not_a_number"}, {"n": "integer"})
        assert isinstance(result, dict)
        assert "n" not in result
        assert "confidence" in result


# ---------------------------------------------------------------------------
# Validation Rules
# ---------------------------------------------------------------------------


class TestValidationRules:
    """Field-level validation rules."""

    def test_valid_payload_passes(self):
        payload = {
            "confidence": 0.85,
            "polymer_type": "HDPE",
            "contamination_pct": 3.5,
        }
        schema = {"polymer_type": "string", "contamination_pct": "float"}
        result = validate_payload(payload, schema)
        assert isinstance(result, dict)
        assert "confidence" in result
        assert result["polymer_type"] == "HDPE"
        assert result["contamination_pct"] == 3.5

    def test_type_mismatch_excluded(self):
        payload = {
            "confidence": 0.85,
            "contamination_pct": "not_a_float",
        }
        schema = {"contamination_pct": "float"}
        result = validate_payload(payload, schema)
        assert "contamination_pct" not in result
        assert "confidence" in result

    def test_extra_fields_kept(self):
        payload = {
            "confidence": 0.85,
            "polymer_type": "HDPE",
            "unknown_field": "discovered",
        }
        # No schema: extra fields pass through
        passthrough = validate_payload(payload, None)
        assert "unknown_field" in passthrough
        assert passthrough["polymer_type"] == "HDPE"
        # With schema: declared valid fields are kept
        schema_result = validate_payload(payload, {"polymer_type": "string"})
        assert schema_result["polymer_type"] == "HDPE"

    def test_empty_payload(self):
        result = validate_payload({}, {})
        assert isinstance(result, dict)
        assert "confidence" in result
        data_fields = {k: v for k, v in result.items() if k != "confidence"}
        assert len(data_fields) == 0

    def test_none_values_excluded(self):
        payload = {
            "confidence": 0.85,
            "x": None,
            "y": "valid",
        }
        result = validate_payload(payload, {"x": "string", "y": "string"})
        assert "x" not in result
        assert result["y"] == "valid"


# ---------------------------------------------------------------------------
# Post-#320 F1: provider confidence is an uncalibrated claim, normalized once.
# Missing or malformed confidence is 0.0, never the old 0.5 default and never a
# NaN/Inf that slips past a threshold comparison.
# ---------------------------------------------------------------------------


class TestProviderConfidenceNormalization:
    def test_f1_01_valid_confidence_is_unchanged(self) -> None:
        assert validate_response({"confidence": 0.60, "x": "a"})["confidence"] == 0.60

    def test_f1_02_missing_confidence_is_zero_not_half(self) -> None:
        assert validate_response({"x": "a"})["confidence"] == 0.0

    @pytest.mark.parametrize("raw", [float("nan"), "nan", "NaN"])
    def test_f1_03_nan_is_zero_and_cannot_clear_a_bar(self, raw) -> None:
        confidence = validate_response({"confidence": raw, "x": "a"})["confidence"]
        assert confidence == 0.0
        assert not confidence >= 0.95

    @pytest.mark.parametrize("raw", [float("inf"), float("-inf"), "inf", True, False])
    def test_f1_04_inf_and_bool_are_zero(self, raw) -> None:
        assert validate_response({"confidence": raw, "x": "a"})["confidence"] == 0.0

    @pytest.mark.parametrize("raw", ["high", "", [], {}, object()])
    def test_f1_05_unparseable_is_zero_without_raising(self, raw) -> None:
        assert validate_response({"confidence": raw, "x": "a"})["confidence"] == 0.0

    def test_f1_06_numeric_string_is_still_accepted(self) -> None:
        assert validate_response({"confidence": "0.75", "x": "a"})["confidence"] == 0.75

    @pytest.mark.parametrize(("raw", "expected"), [(-0.4, 0.0), (1.7, 1.0), ("2", 1.0)])
    def test_f1_07_out_of_range_is_clamped(self, raw, expected) -> None:
        assert validate_response({"confidence": raw, "x": "a"})["confidence"] == expected

    def test_one_normalizer_owns_the_policy(self) -> None:
        """The tracker, validation and consensus all use the same rule."""
        from app.engines.convergence import confidence_tracker
        from app.utils import safe_convert

        assert confidence_tracker.normalize_confidence is safe_convert.normalize_confidence
