"""Inference contract (campaign EIE-NATIVE-CLOSURE-001, lane L1).

The domain-neutral guarantees the inference executors give a converge caller,
pinned at their public surfaces (``InferenceBridge.run`` and ``rule_engine.infer``).
The lane's unit witnesses under ``tests/unit/test_eie_native_e*`` carry the
reproductions; this file states the contract they establish:

C1  A CONTAINS mapping target matches a declared ``list`` of ``map`` value by the
    keys it names; extra element keys neither match nor fail; ``{}`` is no wildcard.
C2  Numeric operators compare only finite, non-boolean numbers.
C3  A derivation with no valid order (a cycle, or a dependency on one) is never
    produced; it is reported blocked with ``cycle``.
C4  A target declared on two nodes is never derived from either declaration.
C5  A fact the entity already carries is never reported as derived.
C6  Unknown stays unknown: no executor invents a value for a missing input.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from app.engines.inference.rule_engine import infer
from app.engines.inference.rule_loader import parse_rules
from app.engines.inference_bridge_adapter import InferenceBridge

_RULES: list[dict[str, Any]] = [
    {
        "rule_id": "c1-kind",
        "conditions": [
            {"field": "parts", "operator": "CONTAINS", "value": {"sku": "X1"}},
            {"field": "rate", "operator": "LT", "value": 3},
        ],
        "outputs": [{"field": "kind", "value_expr": "alpha", "derivation_type": "classification"}],
        "confidence": 0.9,
    }
]


def _graph(props: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"ontology": {"nodes": {"Thing": {"properties": props}}}}


def test_c1_mapping_target_matches_by_named_keys() -> None:
    registry = parse_rules(_RULES, strict=True)
    line = {"parts": [{"sku": "X1", "lot": "7", "side": "in"}], "rate": 1}
    assert infer(line, registry).derived_fields == {"kind": "alpha"}
    other = {"parts": [{"sku": "Y9", "lot": "7"}], "rate": 1}
    assert infer(other, registry).derived_fields == {}


@pytest.mark.parametrize("rate", [-math.inf, math.nan, True, "-Infinity"])
def test_c2_nonfinite_or_boolean_operand_never_satisfies(rate: Any) -> None:
    registry = parse_rules(_RULES, strict=True)
    assert infer({"parts": [{"sku": "X1"}], "rate": rate}, registry).derived_fields == {}


def test_c3_cycle_is_blocked_not_derived() -> None:
    spec = _graph({"a": {"derived_from": ["b"]}, "b": {"derived_from": ["a"]}})
    result = InferenceBridge(domain_spec=spec).run({"a": 1}, {"a": 0.65})
    assert "a" not in result.derived_fields and "b" not in result.derived_fields
    assert result.blocked_fields == {"a": "cycle", "b": "cycle"}


def test_c4_target_on_two_nodes_is_not_derived() -> None:
    spec = {
        "ontology": {
            "nodes": {
                "Plant": {"properties": {"t": {"derived_from": ["p"]}}},
                "Depot": {"properties": {"t": {"derived_from": ["q"]}}},
            }
        }
    }
    result = InferenceBridge(domain_spec=spec).run({"p": 1, "q": 1}, {"p": 0.9, "q": 0.9})
    assert "t" not in result.derived_fields
    assert result.blocked_fields.get("t") == "no_rule"


def test_c5_existing_fact_is_not_reported_as_derived() -> None:
    bridge = InferenceBridge(rules=_RULES, domain_spec=_graph({"g": {"derived_from": ["p"]}}))
    result = bridge.run(
        {"parts": [{"sku": "X1"}], "rate": 1, "kind": "beta", "p": 1, "g": "fact"},
        {"g": 0.95, "p": 0.9},
    )
    assert "kind" not in result.derived_fields
    assert "g" not in result.derived_fields


def test_c6_missing_input_derives_nothing() -> None:
    bridge = InferenceBridge(rules=_RULES, domain_spec=_graph({"g": {"derived_from": ["p", "q"]}}))
    result = bridge.run({"parts": [{"sku": "X1"}]}, {})
    assert result.derived_fields == {}
    assert result.blocked_fields == {"g": "inputs_missing"}
