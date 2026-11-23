"""E2 / F01 — CONTAINS with a mapping target against a list of declared maps.

Defect (reproduced at aabf855): a rule condition ``CONTAINS {code: HDPE}``
compared each list element to the whole target by equality. A cartridge that
declares ``processed_polymers`` as ``list`` of ``map`` with more properties than
the target names (``code``, ``line_id``, ``direction``, ``form``) therefore never
fired on a realistic line, while the bare ``[{code: HDPE}]`` fixture did.

Contract under test (rule_engine._eval_contains): a mapping target matches a
mapping element iff every key the target names is present in the element with
an equal normalised value. Keys the target does not name neither match nor
fail. An empty mapping target is not a wildcard. Scalars and strings keep
their previous semantics.

The cases lead with a domain-neutral synthetic cartridge; PlasticOS is a
regression witness only (domains/plasticos/spec.yaml is read, never edited).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.engines.inference.rule_engine import condition_holds, infer
from app.engines.inference.rule_loader import Operator, RuleCondition, parse_rules
from app.engines.inference_bridge_adapter import STATUS_INVALID, InferenceBridge
from app.engines.native_yaml import parse_native_yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_PLASTICOS = _REPO / "domains" / "plasticos" / "spec.yaml"


def _contains(target: Any) -> RuleCondition:
    return RuleCondition(field="parts", operator=Operator.CONTAINS, value=target)


# ── synthetic, domain-neutral cartridge ───────────────────────────────────────

_SYNTHETIC_RULES: list[dict[str, Any]] = [
    {
        "rule_id": "kind-from-part",
        "conditions": [
            {"field": "parts", "operator": "CONTAINS", "value": {"sku": "X1"}},
            {"field": "defect_rate", "operator": "LT", "value": 3},
        ],
        "outputs": [{"field": "kind", "value_expr": "alpha", "derivation_type": "classification"}],
        "confidence": 0.9,
        "priority": 10,
        "rule_or_model_version": "synthetic-1",
    },
    {
        "rule_id": "band-from-kind",
        "conditions": [
            {"field": "volume", "operator": "GTE", "value": 100},
            {"field": "kind", "operator": "IN", "value": ["alpha"]},
        ],
        "outputs": [{"field": "band", "value_expr": "top", "derivation_type": "classification"}],
        "confidence": 0.8,
        "priority": 5,
        "rule_or_model_version": "synthetic-1",
    },
]


def _synthetic():
    return parse_rules(_SYNTHETIC_RULES, strict=True)


class TestMappingTargetSemantics:
    """R01-R07 at the single-condition level."""

    def test_r01_exact_mapping_element_matches(self) -> None:
        assert condition_holds(_contains({"sku": "X1"}), [{"sku": "X1"}]) is True

    def test_r02_superset_element_matches_without_whole_object_equality(self) -> None:
        line = [{"sku": "X1", "lot": 411, "side": "in"}]
        assert condition_holds(_contains({"sku": "X1"}), line) is True

    def test_r02_match_is_normalised_like_scalar_contains(self) -> None:
        assert condition_holds(_contains({"sku": "x1"}), [{"sku": " X1 ", "lot": "7"}]) is True

    def test_r03_wrong_value_does_not_match(self) -> None:
        line = [{"sku": "Y9", "lot": 411, "side": "in"}]
        assert condition_holds(_contains({"sku": "X1"}), line) is False

    def test_r04_every_target_key_must_match(self) -> None:
        target = {"sku": "X1", "side": "in"}
        assert condition_holds(_contains(target), [{"sku": "X1", "side": "out"}]) is False
        assert condition_holds(_contains(target), [{"sku": "X1", "side": "in", "lot": 1}]) is True

    def test_r04_keys_must_hold_on_one_element_not_across_elements(self) -> None:
        target = {"sku": "X1", "side": "in"}
        parts = [{"sku": "X1", "side": "out"}, {"sku": "Y9", "side": "in"}]
        assert condition_holds(_contains(target), parts) is False

    def test_r05_missing_target_key_does_not_match(self) -> None:
        assert condition_holds(_contains({"sku": "X1"}), [{"lot": 411, "side": "in"}]) is False

    def test_r05_none_valued_target_key_is_not_a_missing_key_match(self) -> None:
        assert condition_holds(_contains({"sku": None}), [{"lot": 411}]) is False

    def test_r06_scalar_list_membership_unchanged(self) -> None:
        assert condition_holds(_contains("HDPE"), ["pp", "hdpe"]) is True
        assert condition_holds(_contains("HDPE"), ["pp"]) is False

    def test_r06_string_substring_unchanged(self) -> None:
        assert condition_holds(_contains("hdpe"), "HDPE, PP") is True
        assert condition_holds(_contains({"sku": "X1"}), "sku X1") is False

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param([], id="empty-list"),
            pytest.param(["X1", "sku"], id="list-of-strings"),
            pytest.param([5, None, 3.2], id="list-of-scalars"),
            pytest.param([["sku", "X1"]], id="nested-list"),
            pytest.param([{"sku": {"code": "X1"}}], id="nested-incompatible-mapping"),
            pytest.param({"sku": "X1"}, id="bare-mapping-not-a-list"),
            pytest.param(5, id="number"),
        ],
    )
    def test_r07_malformed_shapes_never_match_and_never_raise(self, value: Any) -> None:
        assert condition_holds(_contains({"sku": "X1"}), value) is False

    def test_r07_empty_mapping_target_is_not_a_wildcard(self) -> None:
        assert condition_holds(_contains({}), [{"sku": "X1"}]) is False
        assert condition_holds(_contains({}), [{}]) is False


class TestSyntheticCascade:
    """R09-R17 against the domain-neutral cartridge (no PlasticOS names)."""

    def test_realistic_element_fires_rule(self) -> None:
        result = infer(
            {"parts": [{"sku": "X1", "lot": "411", "side": "in"}], "defect_rate": 2.0}, _synthetic()
        )
        assert result.derived_fields == {"kind": "alpha"}

    def test_cascade_runs_in_declared_sequence(self) -> None:
        result = infer(
            {"parts": [{"sku": "X1", "lot": "1"}], "defect_rate": 1, "volume": 150},
            _synthetic(),
        )
        assert [(f.rule_id, f.cascade_level) for f in result.rules_fired] == [
            ("kind-from-part", 0),
            ("band-from-kind", 1),
        ]
        assert result.derived_fields == {"kind": "alpha", "band": "top"}


# ── PlasticOS regression witness (read-only spec) ─────────────────────────────


@pytest.fixture(scope="module")
def plasticos_rules() -> list[dict[str, Any]]:
    return parse_native_yaml(_PLASTICOS.read_bytes())["inference_rules"]


@pytest.fixture
def bridge(plasticos_rules: list[dict[str, Any]]) -> InferenceBridge:
    return InferenceBridge(rules=plasticos_rules)


def _line(code: str = "HDPE", **extra: Any) -> list[dict[str, Any]]:
    # line_id is declared `string` on processed_polymers.items (spec.yaml:55).
    return [{"code": code, "line_id": "11", "direction": "in", **extra}]


class TestPlasticosWitness:
    def test_r18_known_bad_realistic_line_now_grades(self, bridge: InferenceBridge) -> None:
        """Failed at aabf855 (derived nothing); the regression this lane closes."""
        result = bridge.run({"processed_polymers": _line(), "contamination_tolerance": 2.0})
        assert result.derived_fields == {"material_grade": "A"}

    def test_r09_grade_a_with_provenance_and_no_score_gate(self, bridge: InferenceBridge) -> None:
        result = bridge.run(
            {"processed_polymers": _line(), "contamination_tolerance": 2.0},
            {"processed_polymers": 0.95, "contamination_tolerance": 0.9},
        )
        record = result.provenance["material_grade"]
        assert record["rule_id"] == "grade-assignment-hdpe-a"
        assert record["rule_version"] == "plasticos-inference-0.6.0"
        assert record["inputs"]["processed_polymers"] == _line()
        assert record["confidence"] == 0.9
        assert set(result.derived_fields) == {"material_grade"}

    def test_r10_above_a_threshold_falls_to_b_only_when_b_holds(
        self, bridge: InferenceBridge
    ) -> None:
        mid = bridge.run({"processed_polymers": _line(), "contamination_tolerance": 5.0})
        assert mid.derived_fields == {"material_grade": "B"}
        high = bridge.run({"processed_polymers": _line(), "contamination_tolerance": 9.0})
        assert high.derived_fields == {}

    def test_r03_wrong_code_does_not_grade(self, bridge: InferenceBridge) -> None:
        result = bridge.run({"processed_polymers": _line("PP"), "contamination_tolerance": 2.0})
        assert result.derived_fields == {}

    def test_r05_line_without_code_does_not_grade(self, bridge: InferenceBridge) -> None:
        result = bridge.run(
            {
                "processed_polymers": [{"line_id": "11", "direction": "in"}],
                "contamination_tolerance": 2.0,
            }
        )
        assert "material_grade" not in result.derived_fields

    def test_r11_grade_then_tier_in_declared_sequence(self, bridge: InferenceBridge) -> None:
        result = bridge.run(
            {
                "processed_polymers": _line(),
                "contamination_tolerance": 2.0,
                "capacity_tons_month": 500,
            }
        )
        assert result.derived_fields == {"material_grade": "A", "tier": "tier_1"}
        assert result.provenance["material_grade"]["cascade_level"] == 0
        assert result.provenance["tier"]["cascade_level"] == 1

    def test_r12_factual_grade_is_not_overwritten(self, bridge: InferenceBridge) -> None:
        result = bridge.run(
            {
                "processed_polymers": _line(),
                "contamination_tolerance": 2.0,
                "capacity_tons_month": 500,
                "material_grade": "C",
            }
        )
        assert "material_grade" not in result.derived_fields
        # The tier rule reads the factual C, so it does not fire on an inferred A.
        assert "tier" not in result.derived_fields

    def test_r13_confidence_bounded_by_weakest_input_and_rule(
        self, bridge: InferenceBridge
    ) -> None:
        result = bridge.run(
            {"processed_polymers": _line(), "contamination_tolerance": 2.0},
            {"processed_polymers": 0.4, "contamination_tolerance": 0.99},
        )
        assert result.confidence_map["material_grade"] == 0.4
        assert result.confidence_map["material_grade"] <= 0.92

    def test_r14_agreeing_inputs_do_not_boost_confidence(self, bridge: InferenceBridge) -> None:
        """Two inputs at 0.8 yield 0.8: no independence product or promotion."""
        result = bridge.run(
            {"processed_polymers": _line(), "contamination_tolerance": 2.0},
            {"processed_polymers": 0.8, "contamination_tolerance": 0.8},
        )
        assert result.confidence_map["material_grade"] == 0.8

    def test_r15_one_invalid_rule_refuses_the_whole_set(
        self, plasticos_rules: list[dict[str, Any]]
    ) -> None:
        broken = [*plasticos_rules, {"rule_id": "bad", "conditions": [], "outputs": []}]
        bridge = InferenceBridge(rules=broken)
        assert bridge.status == STATUS_INVALID
        result = bridge.run({"processed_polymers": _line(), "contamination_tolerance": 2.0})
        assert result.derived_fields == {}

    def test_r16_replay_is_deterministic_without_double_cascade(
        self, bridge: InferenceBridge
    ) -> None:
        entity = {
            "processed_polymers": _line(),
            "contamination_tolerance": 2.0,
            "capacity_tons_month": 500,
        }
        first = bridge.run(dict(entity))
        second = bridge.run(dict(entity))
        assert first.derived_fields == second.derived_fields
        assert first.provenance == second.provenance
        assert first.rule_trace == second.rule_trace
        assert first.rules_fired == second.rules_fired == 2

    def test_r17_other_cartridge_does_not_leak_grade_or_tier(self) -> None:
        bridge = InferenceBridge(rules=_SYNTHETIC_RULES)
        result = bridge.run(
            {
                "processed_polymers": _line(),
                "contamination_tolerance": 2.0,
                "capacity_tons_month": 500,
            }
        )
        assert result.derived_fields == {}

    def test_r20_later_correction_changes_or_retracts_the_derivation(
        self, bridge: InferenceBridge
    ) -> None:
        """Each pass derives from the facts it is given; the controller retracts absences."""
        passes = [2.0, 5.0, 9.0]
        grades = [
            bridge.run(
                {"processed_polymers": _line(), "contamination_tolerance": c}
            ).derived_fields.get("material_grade")
            for c in passes
        ]
        assert grades == ["A", "B", None]
