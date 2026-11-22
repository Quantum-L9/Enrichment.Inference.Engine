"""L1-F06 — an inference rule applies only to the node it belongs to (adapter half).

Reproduced at 97a26f3: a condition rule had no node coordinate, an authored
``node:`` key was dropped on load, and a rule written for one node fired on an
entity of another node that reused the field name.

Ruled contract (collision authority, 2026-11-22), domain-neutral:

- ``RuleDefinition.node`` carries the optional ``node: <label>`` a rule declares.
- ``InferenceBridge(node=<label>)`` with a spec runs only the rules bound to that
  node (a rule names it, or is the spec's only node's when it names none); a
  rule that cannot be bound refuses the whole set. The derivation graph reads
  only that node's declarations.
- ``InferenceBridge(node=None)`` runs exactly what ran before node binding.
  Runtime filtering activates when the caller passes ``node``; the caller wiring
  (convergence_controller.py / handlers.py) is assigned to EIE-W.
- Spec-level fail-closed validation is ``domain_spec_schema._rules``
  (tests/unit/test_eie_native_f06_spec_contract.py).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.engines.inference.rule_loader import RuleDefinition, bound_node, parse_rules
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


_TWO_RULES = [
    _rule("plant-size", "capacity", "size", node="Plant"),
    _rule("depot-band", "capacity", "band", node="Depot"),
]


# ── rule contract ────────────────────────────────────────────────────────────


def test_rule_definition_carries_its_node() -> None:
    rule = parse_rules([_rule("r", "capacity", "size", node="Plant")], strict=True).get("r")
    assert rule is not None
    assert rule.node == "Plant"
    assert RuleDefinition.model_validate(_rule("r", "capacity", "size")).node is None


def test_bound_node_rules() -> None:
    assert bound_node("r", None, ["Only"]) == "Only"
    assert bound_node("r", "B", ["A", "B"]) == "B"
    with pytest.raises(ValueError, match="names no node"):
        bound_node("r", None, ["A", "B"])
    with pytest.raises(ValueError, match="not declared"):
        bound_node("r", "Z", ["A"])


# ── node given: filtering ────────────────────────────────────────────────────


def test_r19_rule_does_not_fire_on_another_node_reusing_the_field() -> None:
    """R19: the two-node fixture of the retired F06 characterization witness.

    Plant and Depot both declare `capacity`; the rule belongs to Plant and the
    record is a Depot record. The retired witness showed it firing (no node
    binding existed); with the node passed it does not, the owning node still
    fires, and node=None keeps today's behavior on the same fixture.
    """
    rules = [_rule("plant-size", "capacity", "size", node="Plant")]
    spec = _native("Plant", "Depot")
    declaring = [
        n["label"]
        for n in spec["ontology"]["nodes"]
        if any(p["name"] == "capacity" for p in n["properties"])
    ]
    assert declaring == ["Plant", "Depot"]  # the field is genuinely shared
    depot_record = {"capacity": 50.0}
    depot = InferenceBridge(rules=rules, domain_spec=spec, node="Depot")
    assert depot.run(dict(depot_record)).derived_fields == {}
    plant = InferenceBridge(rules=rules, domain_spec=spec, node="Plant")
    assert plant.run(dict(depot_record)).derived_fields == {"size": "large"}
    today = InferenceBridge(rules=rules, domain_spec=spec)
    assert today.run(dict(depot_record)).derived_fields == {"size": "large"}


def test_passing_node_filters_to_that_nodes_rules() -> None:
    spec = _native("Plant", "Depot")
    depot = InferenceBridge(rules=_TWO_RULES, domain_spec=spec, node="Depot")
    assert depot.describe()["rule_count"] == 1
    assert depot.run({"capacity": 50}).derived_fields == {"band": "large"}


def test_passing_node_binds_unnamed_rules_in_a_single_node_spec() -> None:
    bridge = InferenceBridge(
        rules=[_rule("r", "capacity", "size")], domain_spec=_native("Plant"), node="Plant"
    )
    assert bridge.status == STATUS_ACTIVE
    assert bridge.run({"capacity": 50}).derived_fields == {"size": "large"}


@pytest.mark.parametrize(
    "rule",
    [
        pytest.param(_rule("r", "capacity", "size"), id="missing-node-in-multi-node-spec"),
        pytest.param(_rule("r", "capacity", "size", node="Shed"), id="unknown-node"),
    ],
)
def test_passing_node_refuses_a_set_with_an_unbindable_rule(rule: dict[str, Any]) -> None:
    good = _rule("ok", "capacity", "band", node="Plant")
    bridge = InferenceBridge(
        rules=[good, rule], domain_spec=_native("Plant", "Depot"), node="Plant"
    )
    assert bridge.status == STATUS_INVALID
    assert bridge.run({"capacity": 50}).derived_fields == {}
    assert any("node" in error for error in bridge.describe()["errors"])


def test_passing_an_undeclared_node_runs_no_rule() -> None:
    bridge = InferenceBridge(rules=_TWO_RULES, domain_spec=_native("Plant", "Depot"), node="Shed")
    assert bridge.run({"capacity": 50}).derived_fields == {}


# ── node None: exactly today's behavior ──────────────────────────────────────


@pytest.mark.parametrize(
    ("rules", "spec"),
    [
        pytest.param(_TWO_RULES, _native("Plant", "Depot"), id="multi-node-bound-rules"),
        pytest.param([_rule("r", "capacity", "size")], _native("Plant", "Depot"), id="unnamed"),
        pytest.param([_rule("r", "capacity", "size", node="Shed")], _native("Plant"), id="unknown"),
        pytest.param([_rule("r", "capacity", "size")], _native("Plant"), id="single-node"),
    ],
)
def test_node_none_runs_exactly_what_ran_before(
    rules: list[dict[str, Any]], spec: dict[str, Any]
) -> None:
    """No spec-aware filtering and no new refusal: equal to the rules-only bridge."""
    with_spec = InferenceBridge(rules=rules, domain_spec=spec)
    rules_only = InferenceBridge(rules=rules)
    assert with_spec.status == rules_only.status == STATUS_ACTIVE
    assert with_spec.describe()["rule_count"] == rules_only.describe()["rule_count"] == len(rules)
    entity = {"capacity": 50}
    assert with_spec.run(dict(entity)).derived_fields == rules_only.run(dict(entity)).derived_fields


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


# ── derivation graph scoped to the given node (F23 / D11 revisited) ──────────


def _legacy_two_nodes() -> dict[str, Any]:
    return {
        "ontology": {
            "nodes": {
                "Plant": {"properties": {"t": {"derived_from": ["p"]}}},
                "Depot": {"properties": {"t": {"derived_from": ["q"]}}},
            }
        }
    }


def test_d11_given_node_builds_only_its_own_edges() -> None:
    graph = InferenceBridge(domain_spec=_legacy_two_nodes(), node="Depot").graph
    assert graph is not None
    assert [(e.target, e.inputs) for e in graph.edges] == [("t", ("q",))]
    assert graph._conflicts == {}


def test_d11_undeclared_node_builds_no_edges() -> None:
    graph = InferenceBridge(domain_spec=_legacy_two_nodes(), node="Shed").graph
    assert graph is not None
    assert graph.edges == []


def test_d11_node_none_keeps_conflict_blocking() -> None:
    result = InferenceBridge(domain_spec=_legacy_two_nodes()).run(
        {"p": 1, "q": 1}, {"p": 0.9, "q": 0.9}
    )
    assert "t" not in result.derived_fields
    assert result.blocked_fields.get("t") == "no_rule"


# ── spec shapes the node scoping must read (or refuse) without raising ───────


def test_ontology_less_spec_scopes_its_top_level_nodes() -> None:
    """build_derivation_graph also reads `nodes` from the spec root; so does scoping."""
    spec = {
        "nodes": {
            "Plant": {"properties": {"t": {"derived_from": ["p"]}}},
            "Depot": {"properties": {"u": {"derived_from": ["q"]}}},
        }
    }
    graph = InferenceBridge(domain_spec=spec, node="Plant").graph
    assert graph is not None
    assert [e.target for e in graph.edges] == ["t"]


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param({"name": "x", "ontology": "not-a-mapping"}, id="ontology-not-mapping"),
        pytest.param({"name": "x", "ontology": {"nodes": None}}, id="nodes-none"),
    ],
)
def test_malformed_ontology_with_a_node_declares_nothing(spec: dict[str, Any]) -> None:
    bridge = InferenceBridge(rules=[_rule("r", "capacity", "size")], domain_spec=spec, node="Plant")
    assert bridge.status == STATUS_INVALID
    assert bridge.graph is not None
    assert bridge.graph.edges == []
    assert bridge.run({"capacity": 50}).derived_fields == {}
