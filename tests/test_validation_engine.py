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
