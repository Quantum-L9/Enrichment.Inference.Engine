"""Inference node-binding contract (EIE-NATIVE-CLOSURE-001, lane L1-F06).

N1  The spec contract binds every rule to one declared node: the node it names,
    or the spec's only node when it names none. An unnamed rule in a multi-node
    spec, an undeclared node, or a field of another node refuses the spec.
N2  Given a node, the bridge runs only the rules bound to it, refuses a rule set
    it cannot bind, and builds the derivation graph from that node alone.
N3  With no node, the bridge runs exactly what it ran before node binding.
    Runtime filtering activates when the caller passes the node; the caller
    wiring (convergence_controller.py, handlers.py) is assigned to EIE-W.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.engines.domain_spec_schema import validate_spec
from app.engines.inference_bridge_adapter import STATUS_INVALID, InferenceBridge

_TENANT = "n-probe"


def _rule(rule_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "conditions": [{"field": "x", "operator": "EXISTS"}],
        "outputs": [{"field": "y", "value_expr": "v", "derivation_type": "classification"}],
        "confidence": 0.8,
        "rule_or_model_version": "n-1",
        **extra,
    }


def _spec(*labels: str, rules: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "name": _TENANT,
        "version": "1.0.0",
        "ontology": {
            "nodes": [
                {
                    "label": n,
                    "properties": [
                        {"name": "x", "type": "string"},
                        {"name": "y", "type": "string"},
                    ],
                }
                for n in labels
            ]
        },
    }
    if rules is not None:
        spec["inference_rules"] = rules
    return spec


def test_n1_spec_contract_binds_each_rule_to_one_node() -> None:
    validate_spec(_spec("A", rules=[_rule("r")]), _TENANT)
    validate_spec(_spec("A", "B", rules=[_rule("r", node="B")]), _TENANT)
    for bad in ([_rule("r")], [_rule("r", node="Z")]):
        with pytest.raises(ValueError):
            validate_spec(_spec("A", "B", rules=bad), _TENANT)


def test_n2_given_node_filters_and_fails_closed() -> None:
    rules = [_rule("r", node="A")]
    spec = _spec("A", "B")
    assert InferenceBridge(rules=rules, domain_spec=spec, node="A").run({"x": "1"}).derived_fields
    assert (
        not InferenceBridge(rules=rules, domain_spec=spec, node="B").run({"x": "1"}).derived_fields
    )
    unbound = InferenceBridge(rules=[_rule("r")], domain_spec=spec, node="A")
    assert unbound.status == STATUS_INVALID


def test_n2_given_node_graph_reads_only_that_node() -> None:
    spec = {
        "ontology": {
            "nodes": {
                "A": {"properties": {"t": {"derived_from": ["p"]}}},
                "B": {"properties": {"u": {"derived_from": ["q"]}}},
            }
        }
    }
    graph = InferenceBridge(domain_spec=spec, node="B").graph
    assert graph is not None
    assert [e.target for e in graph.edges] == ["u"]


def test_n3_no_node_runs_what_ran_before() -> None:
    rules = [_rule("r", node="A")]
    spec = _spec("A", "B")
    today = InferenceBridge(rules=rules, domain_spec=spec).run({"x": "1"}).derived_fields
    assert today == InferenceBridge(rules=rules).run({"x": "1"}).derived_fields == {"y": "v"}
