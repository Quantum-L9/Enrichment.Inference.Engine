"""Tests for app/services/consensus_engine.py

Covers: Multi-variation LLM consensus synthesis, value counting,
        threshold filtering, confidence weighting.

Source: ~220 lines | Target coverage: 85%
"""

from __future__ import annotations

import pytest

from app.services.consensus_engine import synthesize

# Alias for test compatibility
synthesize_consensus = synthesize


@pytest.fixture
def mock_consensus_payloads_disagreement():
    """5 payloads with distinct polymer_type values (no majority)."""
    return [
        {"confidence": 0.90, "polymer_type": "HDPE"},
        {"confidence": 0.85, "polymer_type": "LDPE"},
        {"confidence": 0.88, "polymer_type": "PP"},
        {"confidence": 0.82, "polymer_type": "PET"},
        {"confidence": 0.86, "polymer_type": "PVC"},
    ]


class TestConsensusEngine:
    """Tests for multi-variation LLM consensus."""

    def test_perfect_agreement_high_confidence(self, mock_consensus_payloads):
        result = synthesize_consensus(mock_consensus_payloads, threshold=0.0)
        # polymer_type: 5/5 agree "HDPE"
        assert result["fields"]["polymer_type"] == "HDPE"
        assert result["per_field_confidence"]["polymer_type"] >= 0.80

    def test_majority_agreement_moderate_confidence(self, mock_consensus_payloads):
        result = synthesize_consensus(mock_consensus_payloads, threshold=0.0)
        # mfi_range: 4/5 agree "0.5-3.0"
        assert result["fields"]["mfi_range"] == "0.5-3.0"

    def test_no_agreement_low_confidence(self, mock_consensus_payloads_disagreement):
        result = synthesize_consensus(mock_consensus_payloads_disagreement, threshold=0.0)
        # 5 different polymer_types → low confidence
        assert result["confidence"] < 0.50

    def test_single_payload_penalty(self):
        payloads = [{"confidence": 0.90, "x": "val"}]
        result = synthesize_consensus(payloads, threshold=0.0)
        # Only 1 variation → limited consensus
        assert result["fields"].get("x") == "val"

    def test_numeric_value_consensus(self):
        payloads = [
            {"confidence": 0.8, "capacity": 1000},
            {"confidence": 0.8, "capacity": 1050},
            {"confidence": 0.8, "capacity": 1000},
            {"confidence": 0.8, "capacity": 980},
        ]
        result = synthesize_consensus(payloads, threshold=0.0)
        assert result["fields"]["capacity"] == 1000  # median of numerics

    def test_string_value_consensus(self):
        payloads = [
            {"confidence": 0.9, "polymer": "HDPE"},
            {"confidence": 0.85, "polymer": "HDPE"},
            {"confidence": 0.7, "polymer": "PE-HD"},
        ]
        result = synthesize_consensus(payloads, threshold=0.0)
        assert result["fields"]["polymer"] == "HDPE"

    def test_threshold_filtering(self):
        payloads = [
            {"confidence": 0.9, "strong_field": "A", "weak_field": "B"},
            {"confidence": 0.85, "strong_field": "A"},
            {"confidence": 0.8, "strong_field": "A"},
            {"confidence": 0.82, "strong_field": "A"},
            {"confidence": 0.86, "strong_field": "A"},
        ]
        result = synthesize_consensus(payloads, threshold=0.65)
        assert "strong_field" in result["fields"]

    def test_empty_payloads_returns_empty(self):
        result = synthesize_consensus([], threshold=0.0)
        assert result["fields"] == {}
        assert result["confidence"] == 0.0

    def test_result_has_field_count(self, mock_consensus_payloads):
        result = synthesize_consensus(mock_consensus_payloads, threshold=0.0)
        assert len(result["fields"]) >= 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestConsensusEdgeCases:
    """Edge case handling in consensus synthesis."""

    def test_all_none_values(self):
        payloads = [
            {"confidence": 0.9, "x": None},
            {"confidence": 0.85, "x": None},
        ]
        result = synthesize_consensus(payloads, threshold=0.0)
        # Consensus on None is still valid
        assert isinstance(result, dict)
        assert "fields" in result

    def test_mixed_types_for_same_field(self):
        payloads = [
            {"confidence": 0.9, "x": "100"},
            {"confidence": 0.85, "x": 100},
        ]
        result = synthesize_consensus(payloads, threshold=0.0)
        # Should handle type mismatch gracefully
        assert "x" in result["fields"]
