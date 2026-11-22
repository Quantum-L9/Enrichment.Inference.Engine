"""L1-F06 — an inference rule applies only to the node it belongs to.

Reproduced at 97a26f3 (tests/unit/test_eie_native_e2_node_boundary.py, now
replaced by these cases): a condition rule had no node coordinate, an authored
``node:`` key was dropped on load, and a rule written for one node fired on an
entity of another node that reused the field name.

Contract under test (domain-neutral; no cartridge or field names in code):

- ``RuleDefinition.node`` carries the optional ``node: <label>`` a rule declares.
- In a spec with exactly one node, a rule without ``node`` binds to that node.
- In a spec with several nodes, every rule names a declared node; a missing or
  unknown node refuses the whole rule set (fail closed, like an invalid rule).
- ``InferenceBridge`` runs only the rules bound to the request's resolved node.
  With several nodes and no resolved node, no condition rule runs.
- The derivation graph is built from the resolved node's declarations only, so
  one target name declared on two nodes is no conflict for a resolved request.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.engines.inference.rule_loader import RuleDefinition, parse_rules
from app.engines.inference_bridge_adapter import (
    STATUS_ACTIVE,
    STATUS_INVALID,
    InferenceBridge,
    node_for_subject,
)

pytestmark = pytest.mark.unit


def _rule(rule_id: str, field: str, out: str, **extra: Any) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "conditions": [{"field": field, "operator": "GT", "value": 10}],
        "outputs": [{"field": out, "value_expr": "large", "derivation_type": "classification"}],
        "confidence": 0.9,
        **extra,
    }


def _native(*labels: str, subjects: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "name": "node-probe",
        "ontology": {
            "nodes": [
                {"label": label, "properties": [{"name": "capacity", "type": "float"}]}
                for label in labels
            ]
        },
    }
    if subjects is not None:
        spec["subjects"] = {"types": subjects}
    return spec


# ── rule contract ────────────────────────────────────────────────────────────


def test_rule_definition_carries_its_node() -> None:
    rule = parse_rules([_rule("r", "capacity", "size", node="Plant")], strict=True).get("r")
    assert rule is not None
    assert rule.node == "Plant"
    assert RuleDefinition.model_validate(_rule("r", "capacity", "size")).node is None


# ── binding ──────────────────────────────────────────────────────────────────


def test_single_node_spec_binds_unnamed_rules_implicitly() -> None:
    bridge = InferenceBridge(rules=[_rule("r", "capacity", "size")], domain_spec=_native("Plant"))
    assert bridge.status == STATUS_ACTIVE
    assert bridge.run({"capacity": 50}).derived_fields == {"size": "large"}


def test_single_node_spec_accepts_an_explicit_matching_node() -> None:
    bridge = InferenceBridge(
        rules=[_rule("r", "capacity", "size", node="Plant")], domain_spec=_native("Plant")
    )
    assert bridge.run({"capacity": 50}).derived_fields == {"size": "large"}


def test_r19_rule_does_not_fire_on_another_node_reusing_the_field() -> None:
    """The reproduced cross-node firing (F06), now refused by binding."""
    rules = [_rule("plant-size", "capacity", "size", node="Plant")]
    spec = _native("Plant", "Depot")
    depot = InferenceBridge(rules=rules, domain_spec=spec, node="Depot")
    assert depot.run({"capacity": 50}).derived_fields == {}
    plant = InferenceBridge(rules=rules, domain_spec=spec, node="Plant")
    assert plant.run({"capacity": 50}).derived_fields == {"size": "large"}


def test_rules_for_each_node_stay_with_their_node() -> None:
    rules = [
        _rule("plant-size", "capacity", "size", node="Plant"),
        _rule("depot-band", "capacity", "band", node="Depot"),
    ]
    spec = _native("Plant", "Depot")
    assert InferenceBridge(rules=rules, domain_spec=spec, node="Depot").run(
        {"capacity": 50}
    ).derived_fields == {"band": "large"}


def test_multi_node_spec_without_resolved_node_runs_no_rule() -> None:
    """Unknown stays unknown: no node is guessed for the request."""
    rules = [_rule("plant-size", "capacity", "size", node="Plant")]
    bridge = InferenceBridge(rules=rules, domain_spec=_native("Plant", "Depot"))
    assert bridge.status == STATUS_ACTIVE
    assert bridge.run({"capacity": 50}).derived_fields == {}
    assert bridge.describe()["rule_count"] == 0


@pytest.mark.parametrize(
    "rule",
    [
        pytest.param(_rule("r", "capacity", "size"), id="missing-node-in-multi-node-spec"),
        pytest.param(_rule("r", "capacity", "size", node="Shed"), id="unknown-node"),
    ],
)
def test_unbound_rule_refuses_the_whole_set(rule: dict[str, Any]) -> None:
    good = _rule("ok", "capacity", "band", node="Plant")
    bridge = InferenceBridge(
        rules=[good, rule], domain_spec=_native("Plant", "Depot"), node="Plant"
    )
    assert bridge.status == STATUS_INVALID
    assert bridge.run({"capacity": 50}).derived_fields == {}
    assert any("node" in error for error in bridge.describe()["errors"])


def test_unknown_node_refused_in_single_node_spec_too() -> None:
    bridge = InferenceBridge(
        rules=[_rule("r", "capacity", "size", node="Shed")], domain_spec=_native("Plant")
    )
    assert bridge.status == STATUS_INVALID


def test_request_node_outside_the_spec_runs_no_rule() -> None:
    rules = [_rule("plant-size", "capacity", "size", node="Plant")]
    bridge = InferenceBridge(rules=rules, domain_spec=_native("Plant", "Depot"), node="Shed")
    assert bridge.run({"capacity": 50}).derived_fields == {}


def test_without_a_spec_rules_run_unbound_as_before() -> None:
    """No spec means no node is knowable; the historical rules-only bridge is unchanged."""
    bridge = InferenceBridge(rules=[_rule("r", "capacity", "size", node="Plant")])
    assert bridge.run({"capacity": 50}).derived_fields == {"size": "large"}


# ── subject → node ───────────────────────────────────────────────────────────


def test_node_for_subject_reads_the_spec_subject_vocabulary() -> None:
    spec = _native(
        "Plant",
        "Depot",
        subjects=[{"name": "site", "node": "Plant"}, {"name": "yard", "node": "Depot"}],
    )
    assert node_for_subject(spec, {"type": "yard", "role": "buyer"}) == "Depot"
    assert node_for_subject(spec, {"type": "unknown"}) is None
    assert node_for_subject(spec, None) is None
    assert node_for_subject(None, {"type": "yard"}) is None
    assert node_for_subject(_native("Plant"), {"type": "yard"}) is None


# ── derivation graph scoped to the resolved node (F23 / D11 revisited) ───────


def _legacy_two_nodes() -> dict[str, Any]:
    return {
        "ontology": {
            "nodes": {
                "Plant": {"properties": {"t": {"derived_from": ["p"]}}},
                "Depot": {"properties": {"t": {"derived_from": ["q"]}}},
            }
        }
    }


def test_d11_resolved_node_builds_only_its_own_edges() -> None:
    bridge = InferenceBridge(domain_spec=_legacy_two_nodes(), node="Depot")
    graph = bridge.graph
    assert graph is not None
    assert [(e.target, e.inputs) for e in graph.edges] == [("t", ("q",))]
    assert graph._conflicts == {}


def test_d11_unresolved_node_keeps_conflict_blocking() -> None:
    bridge = InferenceBridge(domain_spec=_legacy_two_nodes())
    result = bridge.run({"p": 1, "q": 1}, {"p": 0.9, "q": 0.9})
    assert "t" not in result.derived_fields
    assert result.blocked_fields.get("t") == "no_rule"
