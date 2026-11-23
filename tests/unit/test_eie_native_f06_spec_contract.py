"""L1-F06 — the domain spec contract binds every inference rule to one node.

``domain_spec_schema._rules`` (validated at load and publish):

- a rule may name its node with ``node: <label>``; in a single-node spec it may
  omit it and binds to that node;
- in a multi-node spec every rule names a declared node;
- an undeclared node, or a non-identifier node, refuses the spec;
- a rule's condition and output fields are checked against its own node's
  properties, not the union of all nodes.

Before F06 the union of every node's properties was accepted, so a rule could
reference another node's field (reproduced: ``test_field_of_another_node_is_refused``).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.engines.domain_spec_schema import validate_spec

pytestmark = pytest.mark.unit

_TENANT = "node-probe"


def _spec(nodes: dict[str, list[str]], rules: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": _TENANT,
        "version": "1.0.0",
        "ontology": {
            "nodes": [
                {"label": label, "properties": [{"name": f, "type": "float"} for f in fields]}
                for label, fields in nodes.items()
            ]
        },
        "inference_rules": rules,
    }


def _rule(rule_id: str, cond: str, out: str, **extra: Any) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "conditions": [{"field": cond, "operator": "GT", "value": 1}],
        "outputs": [{"field": out, "value_expr": "x", "derivation_type": "classification"}],
        "confidence": 0.9,
        "rule_or_model_version": "probe-1",
        **extra,
    }


_TWO = {"Plant": ["capacity", "size"], "Depot": ["volume", "band"]}


def test_single_node_rule_without_node_validates() -> None:
    validate_spec(_spec({"Plant": ["capacity", "size"]}, [_rule("r", "capacity", "size")]), _TENANT)


def test_single_node_rule_naming_its_node_validates() -> None:
    spec = _spec({"Plant": ["capacity", "size"]}, [_rule("r", "capacity", "size", node="Plant")])
    assert validate_spec(spec, _TENANT)["inference_rules"][0]["node"] == "Plant"


def test_multi_node_rules_naming_their_nodes_validate() -> None:
    rules = [
        _rule("p", "capacity", "size", node="Plant"),
        _rule("d", "volume", "band", node="Depot"),
    ]
    validate_spec(_spec(_TWO, rules), _TENANT)


def test_multi_node_rule_without_node_is_refused() -> None:
    with pytest.raises(ValueError, match="names no node"):
        validate_spec(_spec(_TWO, [_rule("p", "capacity", "size")]), _TENANT)


def test_undeclared_node_is_refused() -> None:
    with pytest.raises(ValueError, match="not declared"):
        validate_spec(_spec(_TWO, [_rule("p", "capacity", "size", node="Shed")]), _TENANT)


@pytest.mark.parametrize("node", [5, "", "not an identifier", ["Plant"]])
def test_malformed_node_is_refused(node: Any) -> None:
    with pytest.raises(ValueError):
        validate_spec(_spec(_TWO, [_rule("p", "capacity", "size", node=node)]), _TENANT)


@pytest.mark.parametrize(
    ("cond", "out"),
    [pytest.param("volume", "size", id="condition"), pytest.param("capacity", "band", id="output")],
)
def test_field_of_another_node_is_refused(cond: str, out: str) -> None:
    """Accepted before F06 (union of all nodes); refused now."""
    with pytest.raises(ValueError, match="undeclared field"):
        validate_spec(_spec(_TWO, [_rule("p", cond, out, node="Plant")]), _TENANT)


def test_spec_without_rules_is_unchanged() -> None:
    spec = _spec(_TWO, [])
    del spec["inference_rules"]
    assert validate_spec(spec, _TENANT) == spec
