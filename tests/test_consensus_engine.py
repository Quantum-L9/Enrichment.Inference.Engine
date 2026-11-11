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


class TestResponseMetadataIsNotAField:
    """`confidence` is attached to every normalized variation as metadata.

    Before the fix it won consensus on every pass and reached consumers as an
    enriched field named "confidence" (seen on the live Odoo -> Gate -> EIE rail).
    """

    def test_confidence_does_not_leak_into_fields(self):
        payloads = [
            {"confidence": 0.9, "polymer_type": "HDPE"},
            {"confidence": 0.8, "polymer_type": "HDPE"},
            {"confidence": 0.7, "polymer_type": "HDPE"},
        ]
        result = synthesize(payloads, threshold=0.5)
        assert "confidence" not in result["fields"]
        assert "confidence" not in result["per_field_confidence"]
        assert result["fields"]["polymer_type"] == "HDPE"
        assert isinstance(result["confidence"], float)


class TestTypePreservingConsensus:
    """WU-07 / F-004: median only for genuine numbers; strings and bools keep type."""

    def test_zip_string_stays_a_string(self):
        result = synthesize([{"zip": "02139"}, {"zip": "02139"}], threshold=0.0, total_attempted=2)
        assert result["fields"]["zip"] == "02139"
        assert isinstance(result["fields"]["zip"], str)

    def test_bool_stays_a_bool(self):
        result = synthesize([{"flag": True}, {"flag": True}], threshold=0.0, total_attempted=2)
        assert result["fields"]["flag"] is True

    def test_int_stays_an_int(self):
        result = synthesize([{"n": 150}, {"n": 150}], threshold=0.0, total_attempted=2)
        assert result["fields"]["n"] == 150
        assert isinstance(result["fields"]["n"], int)

    def test_float_values_still_use_median(self):
        result = synthesize([{"v": 1.0}, {"v": 2.0}, {"v": 9.0}], threshold=0.0, total_attempted=3)
        assert result["fields"]["v"] == 2.0

    def test_int_median_between_two_values_is_not_truncated(self):
        result = synthesize([{"n": 1}, {"n": 2}], threshold=0.0, total_attempted=2)
        assert result["fields"]["n"] == 1.5

    def test_numeric_strings_use_mode_not_median(self):
        result = synthesize(
            [{"code": "007"}, {"code": "007"}, {"code": "7"}],
            threshold=0.0,
            total_attempted=3,
        )
        assert result["fields"]["code"] == "007"


class TestEmptyValuesAreNotEnriched:
    """WU-07 / F-025: None and '' never become enriched fields."""

    def test_all_none_synthesizes_no_fields(self):
        result = synthesize([{"x": None}, {"x": None}], threshold=0.0, total_attempted=2)
        assert result["fields"] == {}
        assert result["per_field_confidence"] == {}
        assert result["confidence"] == 0.0

    def test_empty_string_is_not_a_field(self):
        result = synthesize([{"x": ""}, {"x": ""}], threshold=0.0, total_attempted=2)
        assert result["fields"] == {}

    def test_empties_do_not_count_toward_agreement(self):
        result = synthesize(
            [{"x": "HDPE"}, {"x": None}, {"x": None}], threshold=0.6, total_attempted=3
        )
        assert "x" not in result["fields"]


# ── #320 F4 (revised): partial field_thresholds maps ─────────────────────────


class TestPartialFieldThresholds:
    """A requested field the map omits inherits the explicit default bar."""

    @staticmethod
    def _filter(variations, field_thresholds, default_threshold=0.85):
        from app.services.consensus_engine import apply_field_confidence_thresholds

        return apply_field_confidence_thresholds(
            variations,
            field_thresholds,
            requested_fields=["street", "city", "website"],
            default_threshold=default_threshold,
        )

    def test_explicit_override_is_enforced_and_omitted_field_inherits_default(self) -> None:
        kept = self._filter(
            [{"confidence": 0.90, "street": "1 Main", "city": "Austin", "website": "a.io"}],
            {"street": 0.95},
        )
        # street must clear its own 0.95; city and website clear the 0.85 default.
        assert kept == [{"confidence": 0.90, "city": "Austin", "website": "a.io"}]

    @pytest.mark.parametrize(("confidence", "kept"), [(0.84, False), (0.85, True)])
    def test_omitted_field_cannot_pass_below_the_default(self, confidence, kept) -> None:
        result = self._filter([{"confidence": confidence, "city": "Austin"}], {"street": 0.95})
        assert ("city" in result[0]) is kept

    def test_explicit_field_cannot_pass_below_its_override(self) -> None:
        result = self._filter([{"confidence": 0.949, "street": "1 Main"}], {"street": 0.95})
        assert "street" not in result[0]

    def test_unsolicited_field_is_still_dropped(self) -> None:
        result = self._filter([{"confidence": 0.99, "revenue": 10, "city": "Austin"}], {})
        assert result == [{"confidence": 0.99, "city": "Austin"}]

    def test_no_default_keeps_the_strict_named_only_filter(self) -> None:
        from app.services.consensus_engine import apply_field_confidence_thresholds

        result = apply_field_confidence_thresholds(
            [{"confidence": 0.99, "street": "1 Main", "city": "Austin"}],
            {"street": 0.95},
        )
        assert result == [{"confidence": 0.99, "street": "1 Main"}]

    def test_agreement_gate_still_applies_after_the_field_bar(self) -> None:
        """consensus_threshold stays an independent gate, not the fallback."""
        filtered = self._filter(
            [
                {"confidence": 0.95, "city": "Austin"},
                {"confidence": 0.95, "city": "Dallas"},
                {"confidence": 0.95, "city": "Houston"},
            ],
            {"street": 0.95},
        )
        assert all("city" in v for v in filtered)  # every reply clears 0.85
        result = synthesize(filtered, threshold=0.65, total_attempted=len(filtered))
        assert "city" not in result["fields"]  # 1/3 agreement < 0.65


# ── Post-#320 F1: agreement never manufactures confidence ────────────────────


class TestSelectedValueScore:
    """per_field_confidence is a bounded uncalibrated heuristic.

    It never exceeds the strongest raw assertion behind the selected value,
    duplication never raises it, and dissent among distinct values lowers it.
    """

    @staticmethod
    def _score(variations, field="city", threshold=0.6):
        result = synthesize(variations, threshold=threshold, total_attempted=len(variations))
        return result["fields"].get(field), result["per_field_confidence"].get(field)

    def test_f1_08_single_060_reply_stays_060(self) -> None:
        value, score = self._score([{"confidence": 0.60, "city": "Austin"}])
        assert value == "Austin"
        assert score == pytest.approx(0.60)

    def test_f1_09_single_095_reply_is_not_upgraded_to_one(self) -> None:
        _, score = self._score([{"confidence": 0.95, "city": "Austin"}])
        assert score == pytest.approx(0.95)
        assert score < 1.0

    def test_f1_10_zero_raw_confidence_stays_zero(self) -> None:
        _, score = self._score([{"confidence": 0.0, "city": "Austin"}])
        assert score == 0.0

    def test_f1_11_losing_high_confidence_does_not_upgrade_the_winner(self) -> None:
        variations = [
            {"confidence": 0.50, "city": "Austin"},
            {"confidence": 0.50, "city": "Austin"},
            {"confidence": 0.99, "city": "Dallas"},
        ]
        value, score = self._score(variations, threshold=0.6)
        assert value == "Austin"
        assert score <= 0.50

    def test_f1_12_duplication_does_not_raise_the_score(self) -> None:
        one = [{"confidence": 0.70, "city": "Austin"}, {"confidence": 0.90, "city": "Dallas"}]
        many = [{"confidence": 0.70, "city": "Austin"}] * 10 + [
            {"confidence": 0.90, "city": "Dallas"}
        ]
        _, single_score = self._score(one, threshold=0.0)
        value, dup_score = self._score(many, threshold=0.0)
        assert value == "Austin"
        assert dup_score <= single_score

    def test_f1_12_unanimous_duplicates_score_as_one_observation(self) -> None:
        _, score = self._score([{"confidence": 0.70, "city": "Austin"}] * 5)
        assert score == pytest.approx(0.70)

    def test_f1_13_distinct_citations_do_not_create_independence(self) -> None:
        """Response-level citations are not per-field provenance; no id is invented."""
        plain = [{"confidence": 0.7, "city": "Austin"}] * 3
        cited = [
            {"confidence": 0.7, "city": "Austin", "citations": [f"https://s{i}.example"]}
            for i in range(3)
        ]
        _, plain_score = self._score(plain)
        _, cited_score = self._score(cited)
        assert cited_score == plain_score == pytest.approx(0.7)

    def test_f1_14_synthetic_median_is_capped_by_its_contributors(self) -> None:
        variations = [
            {"confidence": 0.9, "mfi": 1.0},
            {"confidence": 0.4, "mfi": 2.0},
        ]
        value, score = self._score(variations, field="mfi", threshold=0.0)
        assert value == 1.5  # median, observed in no reply
        assert score <= 0.4

    @pytest.mark.parametrize(
        ("value", "expected_type"),
        [(0, int), (False, bool), ("02139", str), (2.5, float)],
    )
    def test_f1_15_types_and_values_are_retained(self, value, expected_type) -> None:
        got, _ = self._score([{"confidence": 0.8, "f": value}], field="f")
        assert got == value
        assert type(got) is expected_type

    def test_f1_16_empty_values_are_not_enriched(self) -> None:
        result = synthesize(
            [{"confidence": 0.9, "city": None}, {"confidence": 0.9, "city": ""}],
            threshold=0.0,
            total_attempted=2,
        )
        assert result["fields"] == {}
        assert result["confidence"] == 0.0

    def test_agreement_floor_no_longer_admits_dissent(self) -> None:
        """The removed 0.2 floor let any value pass a threshold at or below 0.2."""
        variations = [{"confidence": 0.9, "city": c} for c in ("A", "B", "C", "D", "E", "F")]
        result = synthesize(variations, threshold=0.2, total_attempted=len(variations))
        assert "city" not in result["fields"]

    def test_f1_17_field_threshold_judges_the_effective_score(self) -> None:
        """Agreement among 0.96 replies with dissent cannot clear a 0.95 field bar."""
        from app.services.consensus_engine import apply_field_score_thresholds

        variations = [
            {"confidence": 0.96, "city": "Austin"},
            {"confidence": 0.96, "city": "Austin"},
            {"confidence": 0.96, "city": "Dallas"},
        ]
        synthesis = synthesize(variations, threshold=0.6, total_attempted=3)
        assert synthesis["fields"]["city"] == "Austin"
        gated = apply_field_score_thresholds(
            synthesis, {"city": 0.95}, requested_fields=["city"], default_threshold=0.85
        )
        assert "city" not in gated["fields"]
        assert gated["per_field_confidence"] == {}

    def test_field_score_gate_uses_the_resolved_default(self) -> None:
        from app.services.consensus_engine import apply_field_score_thresholds

        synthesis = synthesize(
            [{"confidence": 0.84, "city": "Austin", "street": "1 Main"}],
            threshold=0.6,
            total_attempted=1,
        )
        gated = apply_field_score_thresholds(
            synthesis, {"street": 0.5}, requested_fields=["city", "street"], default_threshold=0.85
        )
        assert gated["fields"] == {"street": "1 Main"}
        assert gated["confidence"] == pytest.approx(0.84)

    @pytest.mark.parametrize("raw", [float("nan"), "nan", True, "high"])
    def test_malformed_raw_confidence_cannot_clear_a_field_bar(self, raw) -> None:
        from app.services.consensus_engine import apply_field_confidence_thresholds

        kept = apply_field_confidence_thresholds(
            [{"confidence": raw, "city": "Austin"}], {"city": 0.5}
        )
        assert "city" not in kept[0]
