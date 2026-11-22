"""E2 / F18 — numeric operators admit only finite, non-boolean numbers.

Defect (reproduced at aabf855): ``rule_engine._finite_floats`` coerced with
``float()`` and never checked finiteness, despite its name. ``-inf`` therefore
satisfied ``contamination_tolerance LT 3`` (grading a line A), ``inf`` or the
provider string ``"Infinity"`` satisfied ``capacity_tons_month GTE 416.67``, and
``True`` compared as ``1.0``.

Reachability: ``EnrichRequest.entity`` is ``dict[str, Any]`` and Python's JSON
decoder accepts ``Infinity`` / ``NaN`` literals, so the request's own record can
carry them; provider replies are untyped on the path into ``bridge.run`` (see
convergence_controller's merge of ``pass_result.enriched_fields``). Nothing
filters them before the rule engine. ``spec_types._check_float`` already
defines a float as a finite, non-boolean number; the engine now agrees.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from app.engines.inference.rule_engine import condition_holds, infer
from app.engines.inference.rule_loader import Operator, RuleCondition, parse_rules

pytestmark = pytest.mark.unit

_NUMERIC = [Operator.GT, Operator.LT, Operator.GTE, Operator.LTE]

_NONFINITE = [
    pytest.param(math.nan, id="nan"),
    pytest.param(math.inf, id="inf"),
    pytest.param(-math.inf, id="-inf"),
    pytest.param("Infinity", id="str-Infinity"),
    pytest.param("-inf", id="str--inf"),
    pytest.param("nan", id="str-nan"),
    pytest.param(True, id="bool-true"),
    pytest.param(False, id="bool-false"),
]


@pytest.mark.parametrize("value", _NONFINITE)
@pytest.mark.parametrize("operator", _NUMERIC)
def test_r08_nonfinite_or_boolean_value_never_satisfies(operator: Operator, value: Any) -> None:
    condition = RuleCondition(field="f", operator=operator, value=416.67)
    assert condition_holds(condition, value) is False


@pytest.mark.parametrize("operator", _NUMERIC)
def test_r08_nonfinite_target_never_satisfies(operator: Operator) -> None:
    condition = RuleCondition(field="f", operator=operator, value=math.inf)
    assert condition_holds(condition, 5) is False


@pytest.mark.parametrize(
    ("operator", "value", "expected"),
    [
        (Operator.GT, 500, True),
        (Operator.GT, 1.5, False),
        (Operator.GTE, "416.67", True),
        (Operator.LT, 2, True),
        (Operator.LTE, 416.67, True),
    ],
)
def test_finite_numbers_keep_their_semantics(
    operator: Operator, value: Any, expected: bool
) -> None:
    condition = RuleCondition(field="f", operator=operator, value=416.67)
    assert condition_holds(condition, value) is expected


def test_r08_json_infinity_in_a_record_does_not_derive() -> None:
    """The literal a request body can carry, through the public engine entry."""
    entity = json.loads('{"defect_rate": -Infinity, "volume": Infinity}')
    registry = parse_rules(
        [
            {
                "rule_id": "low-defect",
                "conditions": [{"field": "defect_rate", "operator": "LT", "value": 3}],
                "outputs": [
                    {"field": "kind", "value_expr": "a", "derivation_type": "classification"}
                ],
                "confidence": 0.9,
            },
            {
                "rule_id": "big",
                "conditions": [{"field": "volume", "operator": "GTE", "value": 100}],
                "outputs": [
                    {"field": "band", "value_expr": "top", "derivation_type": "classification"}
                ],
                "confidence": 0.9,
            },
        ],
        strict=True,
    )
    assert infer(entity, registry).derived_fields == {}
