"""L1-F06 — PlasticOS inference output is identical before and after node binding.

PlasticOS declares one node and its rules name none, so every rule binds to
that node implicitly and no spec edit is needed. The witness compares the
node-bound bridge (spec supplied: the production path) with the rules-only
bridge, which binds nothing and so runs exactly the pre-binding rule set, over a
grid of realistic facts, and pins the expected grades. The spec is read only.
"""

from __future__ import annotations

from itertools import product
from pathlib import Path
from typing import Any

import pytest

from app.engines.inference_bridge_adapter import InferenceBridge
from app.engines.native_yaml import parse_native_yaml

pytestmark = pytest.mark.unit

_SPEC = Path(__file__).resolve().parents[2] / "domains" / "plasticos" / "spec.yaml"


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return parse_native_yaml(_SPEC.read_bytes())


def _entities() -> list[dict[str, Any]]:
    lines = [
        [{"code": "HDPE", "line_id": "11", "direction": "in"}],
        [{"code": "PP", "line_id": "12", "direction": "in"}],
        [{"line_id": "13"}],
    ]
    contamination = [1.0, 3.0, 5.0, 7.0, 9.0]
    capacity = [None, 100, 500]
    grade = [None, "C"]
    out = []
    for line, c, cap, g in product(lines, contamination, capacity, grade):
        entity: dict[str, Any] = {"processed_polymers": line, "contamination_tolerance": c}
        if cap is not None:
            entity["capacity_tons_month"] = cap
        if g is not None:
            entity["material_grade"] = g
        out.append(entity)
    return out


def test_bound_and_unbound_bridges_agree_on_every_entity(spec: dict[str, Any]) -> None:
    rules = spec["inference_rules"]
    bound = InferenceBridge(rules=rules, domain_spec=spec)
    unbound = InferenceBridge(rules=rules)
    assert bound.describe()["rule_count"] == unbound.describe()["rule_count"] == 3
    for entity in _entities():
        b, u = bound.run(dict(entity)), unbound.run(dict(entity))
        assert b.derived_fields == u.derived_fields, entity
        assert b.provenance == u.provenance, entity
        assert b.confidence_map == u.confidence_map, entity


@pytest.mark.parametrize(
    ("contamination", "capacity", "expected"),
    [
        (1.0, 500, {"material_grade": "A", "tier": "tier_1"}),
        (1.0, 100, {"material_grade": "A"}),
        (5.0, 500, {"material_grade": "B"}),
        (9.0, 500, {}),
    ],
)
def test_pinned_plasticos_outputs(
    spec: dict[str, Any], contamination: float, capacity: int, expected: dict[str, str]
) -> None:
    bridge = InferenceBridge(rules=spec["inference_rules"], domain_spec=spec)
    entity = {
        "processed_polymers": [{"code": "HDPE", "line_id": "11", "direction": "in"}],
        "contamination_tolerance": contamination,
        "capacity_tons_month": capacity,
    }
    assert bridge.run(entity).derived_fields == expected
