"""E2 / F06 — evidence for BLOCKED_SEMANTIC_OWNER_DECISION (node boundary).

Reproduced at aabf855 with a two-node cartridge that reuses one property name:
a condition rule has no node coordinate, so it fires on any entity carrying the
field, whichever node the entity is. No existing binding distinguishes the
owner: ``RuleDefinition`` declares no node/label field and, under pydantic's
default ``extra="ignore"``, a ``node:`` key an author writes is dropped on load.

This lane does not add a node binding (lane brief: "do not add node_id"); the
semantic owner of the ``inference_rules`` contract decides. These tests pin the
reproduced facts so the decision has an exact witness. They are not an
endorsement of cross-node firing: when the owner admits a binding, they are
replaced by the positive/negative pair that binding defines.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.engines.inference.rule_engine import infer
from app.engines.inference.rule_loader import RuleDefinition, parse_rules

pytestmark = pytest.mark.unit

# Two nodes, one shared property name. The rule's author means node "Plant".
_SPEC: dict[str, Any] = {
    "name": "two-node-probe",
    "ontology": {
        "nodes": [
            {"label": "Plant", "properties": [{"name": "capacity", "type": "float"}]},
            {"label": "Depot", "properties": [{"name": "capacity", "type": "float"}]},
        ]
    },
}

_RULE: dict[str, Any] = {
    "rule_id": "plant-size",
    "node": "Plant",  # what an author would write; no contract admits it
    "conditions": [{"field": "capacity", "operator": "GT", "value": 10}],
    "outputs": [{"field": "size", "value_expr": "large", "derivation_type": "classification"}],
    "confidence": 0.9,
}


def test_r19_rule_contract_has_no_node_coordinate() -> None:
    assert not {"node", "node_label", "label", "node_id"} & set(RuleDefinition.model_fields)


def test_r19_authored_node_key_is_not_retained() -> None:
    rule = parse_rules([_RULE], strict=True).get("plant-size")
    assert rule is not None
    assert "node" not in rule.model_dump()


def test_r19_rule_fires_on_the_other_node_reusing_the_field() -> None:
    """The reproduced cross-node firing the owner decision must resolve."""
    declaring = [
        node["label"]
        for node in _SPEC["ontology"]["nodes"]
        if any(p["name"] == "capacity" for p in node["properties"])
    ]
    assert declaring == ["Plant", "Depot"]  # the field is genuinely shared
    depot_entity = {"capacity": 50.0}  # a Depot record
    result = infer(depot_entity, parse_rules([_RULE], strict=True))
    assert result.derived_fields == {"size": "large"}
