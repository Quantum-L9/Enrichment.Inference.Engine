"""Inference node-binding contract (EIE-NATIVE-CLOSURE-001, lane L1-F06).

N1  A rule belongs to exactly one ontology node: the node it names, or the
    spec's only node when it names none.
N2  A rule that cannot be placed on one declared node refuses the whole rule
    set (fail closed), so nothing runs.
N3  Condition rules run only for the request's resolved node; with several
    nodes and no resolved node, none runs.
N4  The derivation graph for a resolved request reads only that node.
"""

from __future__ import annotations

from typing import Any

from app.engines.inference_bridge_adapter import STATUS_INVALID, InferenceBridge


def _rule(rule_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "conditions": [{"field": "x", "operator": "EXISTS"}],
        "outputs": [{"field": "y", "value_expr": "v", "derivation_type": "classification"}],
        "confidence": 0.8,
        **extra,
    }


def _spec(*labels: str) -> dict[str, Any]:
    return {
        "ontology": {
            "nodes": [{"label": n, "properties": [{"name": "x", "type": "string"}]} for n in labels]
        }
    }


def test_n1_single_node_binds_implicitly() -> None:
    assert InferenceBridge(rules=[_rule("r")], domain_spec=_spec("A")).run(
        {"x": "1"}
    ).derived_fields == {"y": "v"}


def test_n2_unplaceable_rule_refuses_the_set() -> None:
    assert InferenceBridge(rules=[_rule("r")], domain_spec=_spec("A", "B")).status == STATUS_INVALID
    assert (
        InferenceBridge(rules=[_rule("r", node="Z")], domain_spec=_spec("A")).status
        == STATUS_INVALID
    )


def test_n3_rules_run_only_for_the_resolved_node() -> None:
    rules = [_rule("r", node="A")]
    spec = _spec("A", "B")
    assert InferenceBridge(rules=rules, domain_spec=spec, node="A").run({"x": "1"}).derived_fields
    assert (
        not InferenceBridge(rules=rules, domain_spec=spec, node="B").run({"x": "1"}).derived_fields
    )
    assert not InferenceBridge(rules=rules, domain_spec=spec).run({"x": "1"}).derived_fields


def test_n4_graph_reads_only_the_resolved_node() -> None:
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
