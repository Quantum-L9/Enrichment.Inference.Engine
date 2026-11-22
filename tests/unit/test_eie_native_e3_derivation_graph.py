"""E3 — derivation-graph cycles (F03), native tenant name (F07), derived_from scope (F02/F23).

F03 (reproduced at aabf855): ``_topo_sort`` appended every cycle member to the
executable order, so ``run_inference`` evaluated them like any other target. A
self-dependent ``a <- a`` whose own value sat between ``confidence_floor`` and
``existing_threshold`` was then "derived" from itself and reported DERIVED.
Cycle members, and every target that depends on one, now have no valid
derivation: they leave the executable order and come back on the existing
``blocked`` surface with the existing ``InferenceStatus.CYCLE``, naming the
inputs that cannot be resolved. The executable order is also declaration-ordered
rather than set-ordered, so it no longer depends on PYTHONHASHSEED.

F07 (reproduced): the graph's ``domain`` (logged on every build and run) read
only ``domain`` / ``metadata.domain``. A native cartridge names itself with
top-level ``name`` and was reported ``unknown``. Legacy keys keep precedence.

F02/F23 (evidence, no code change): no native contract admits ``derived_from``.
``app/engines/domain_spec_schema.py::_properties`` and
``app/engines/spec_types.py::validate_declaration`` neither declare nor give it
meaning, and ``domains/plasticos/spec.yaml`` carries none. The native
list-shaped property form therefore builds no derivation edge, and the
dict-shaped legacy form keeps its behavior; both are pinned here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.engines import inference_bridge_v2 as v2
from app.engines.inference_bridge_adapter import InferenceBridge
from app.engines.inference_bridge_v2 import InferenceStatus, build_derivation_graph, run_inference

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]


def _legacy(props: dict[str, dict[str, Any]], **top: Any) -> dict[str, Any]:
    """Dict-shaped legacy ontology: nodes.{Label}.properties.{name}: {...}."""
    return {**top, "ontology": {"nodes": {"Thing": {"properties": props}}}}


def _edge(*inputs: str, rule: str = "probe", floor: float = 0.6) -> dict[str, Any]:
    return {
        "type": "string",
        "derived_from": list(inputs),
        "inference_rule": rule,
        "confidence_floor": floor,
    }


@pytest.fixture
def probe_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real rule that derives a value whenever it is executed."""

    def _rule(inputs: dict[str, Any], _edge: v2.DerivationEdge) -> tuple[Any, float]:
        return f"from:{','.join(sorted(inputs))}", 0.95

    monkeypatch.setitem(v2._RULE_REGISTRY, "probe", _rule)


# ── F03: cycles ──────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("probe_rule")
class TestCycles:
    def test_d06_self_dependent_target_is_not_derived_from_itself(self) -> None:
        graph = build_derivation_graph(_legacy({"a": _edge("a")}))
        result = run_inference(graph, {"a": "seed"}, {"a": 0.65})
        assert "a" not in result.derived
        assert result.blocked["a"].status is InferenceStatus.CYCLE
        assert result.blocked["a"].missing_inputs == ["a"]
        assert result.blocked["a"].value is None
        assert "a" not in graph.topo_order

    def test_d07_two_cycle_is_refused_deterministically(self) -> None:
        graph = build_derivation_graph(_legacy({"a": _edge("b"), "b": _edge("a")}))
        first = run_inference(graph, {"a": "seed"}, {"a": 0.65})
        second = run_inference(graph, {"a": "seed"}, {"a": 0.65})
        for result in (first, second):
            assert result.derived == {}
            assert {k: r.status for k, r in result.blocked.items()} == {
                "a": InferenceStatus.CYCLE,
                "b": InferenceStatus.CYCLE,
            }
            assert result.blocked["a"].missing_inputs == ["b"]
            assert result.blocked["b"].missing_inputs == ["a"]
        assert list(first.blocked) == list(second.blocked)

    def test_d08_independent_target_survives_a_cycle(self) -> None:
        graph = build_derivation_graph(
            _legacy({"a": _edge("b"), "b": _edge("a"), "c": _edge("x"), "x": {"type": "string"}})
        )
        result = run_inference(graph, {"x": "v"}, {"x": 0.9})
        assert result.derived["c"].status is InferenceStatus.DERIVED
        assert result.derived["c"].value == "from:x"
        assert graph.topo_order == ["c"]
        assert {result.blocked[k].status for k in ("a", "b")} == {InferenceStatus.CYCLE}

    def test_dependent_of_a_cycle_is_blocked_naming_the_cycle_input(self) -> None:
        graph = build_derivation_graph(
            _legacy({"a": _edge("b"), "b": _edge("a"), "d": _edge("a", "x")})
        )
        result = run_inference(graph, {"x": "v"}, {"x": 0.9})
        assert result.blocked["d"].status is InferenceStatus.CYCLE
        assert result.blocked["d"].missing_inputs == ["a"]

    def test_cycle_is_reported_through_the_adapter_surface(self) -> None:
        bridge = InferenceBridge(domain_spec=_legacy({"a": _edge("a")}))
        result = bridge.run({"a": "seed"}, {"a": 0.65})
        assert "a" not in result.derived_fields
        assert result.blocked_fields == {"a": "cycle"}


@pytest.mark.usefixtures("probe_rule")
class TestAcyclicBehaviourPreserved:
    def test_d05_two_hop_chain_orders_and_derives(self) -> None:
        graph = build_derivation_graph(
            _legacy({"c": _edge("b"), "b": _edge("a"), "a": {"type": "string"}})
        )
        assert graph.topo_order == ["b", "c"]
        result = run_inference(graph, {"a": "v"}, {"a": 0.9})
        assert [k for k, r in result.derived.items() if r.status is InferenceStatus.DERIVED] == [
            "b",
            "c",
        ]

    def test_d09_missing_input_is_named(self) -> None:
        graph = build_derivation_graph(_legacy({"t": _edge("p", "q")}))
        result = run_inference(graph, {"p": 1}, {"p": 0.9})
        assert result.blocked["t"].status is InferenceStatus.INPUTS_MISSING
        assert result.blocked["t"].missing_inputs == ["q"]

    def test_d10_input_below_floor_is_not_derived(self) -> None:
        graph = build_derivation_graph(_legacy({"t": _edge("p", floor=0.8)}))
        result = run_inference(graph, {"p": 1}, {"p": 0.5})
        assert result.blocked["t"].status is InferenceStatus.BELOW_THRESHOLD
        assert "t" not in result.derived


_ORDER_PROBE = """
import json, sys
from app.engines.inference_bridge_v2 import build_derivation_graph
props = {f"t{i}": {"derived_from": [f"in{i}"]} for i in range(12)}
props["z"] = {"derived_from": ["t3", "t7"]}
g = build_derivation_graph({"ontology": {"nodes": {"N": {"properties": props}}}})
sys.stdout.write(chr(10) + "ORDER=" + json.dumps(g.topo_order) + chr(10))
"""


def test_d13_executable_order_does_not_depend_on_hash_seed() -> None:
    orders = []
    for seed in ("1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = (
            subprocess.run(
                [sys.executable, "-c", _ORDER_PROBE],
                cwd=_REPO,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            .stdout.split("ORDER=", 1)[1]
            .splitlines()[0]
        )
        orders.append(json.loads(out))
    assert orders[0] == orders[1] == orders[2]
    assert orders[0] == [*(f"t{i}" for i in range(12)), "z"]


# ── F07: native tenant name ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        pytest.param({"name": "acme", "version": "1.0.0"}, "acme", id="d04-native-name"),
        pytest.param({"domain": "legacy", "name": "acme"}, "legacy", id="legacy-domain-wins"),
        pytest.param(
            {"metadata": {"domain": "meta"}, "name": "acme"}, "meta", id="legacy-metadata"
        ),
        pytest.param({}, "unknown", id="nothing-declared"),
    ],
)
def test_d04_graph_names_the_declaring_tenant(spec: dict[str, Any], expected: str) -> None:
    assert build_derivation_graph({**spec, "ontology": {"nodes": []}}).domain == expected


def test_d04_plasticos_native_spec_is_named() -> None:
    from app.engines.native_yaml import parse_native_yaml

    spec = parse_native_yaml((_REPO / "domains" / "plasticos" / "spec.yaml").read_bytes())
    assert build_derivation_graph(spec).domain == "plasticos"


# ── F02 / F23: derived_from has no admitting native contract ─────────────────


def test_d01_native_list_property_derived_from_builds_no_edge() -> None:
    """No contract gives native `derived_from` meaning; none is invented."""
    spec = {
        "name": "native-probe",
        "ontology": {
            "nodes": [
                {
                    "label": "Thing",
                    "properties": [
                        {"name": "a", "type": "float"},
                        {"name": "b", "type": "float", "derived_from": ["a"]},
                    ],
                }
            ]
        },
    }
    graph = build_derivation_graph(spec)
    assert graph.edges == []
    assert graph.topo_order == []


def test_f02_native_schema_owners_do_not_mention_derived_from() -> None:
    for owner in ("app/engines/domain_spec_schema.py", "app/engines/spec_types.py"):
        assert "derived_from" not in (_REPO / owner).read_text(encoding="utf-8"), owner
    assert "derived_from" not in (_REPO / "domains/plasticos/spec.yaml").read_text(encoding="utf-8")


@pytest.mark.usefixtures("probe_rule")
def test_d03_legacy_dict_shaped_graph_is_unchanged() -> None:
    graph = build_derivation_graph(
        _legacy(
            {"p": {"type": "string"}, "g": _edge("p"), "t": _edge("g", "p")},
            domain="legacy-probe",
        )
    )
    assert [(e.target, e.inputs) for e in graph.edges] == [("g", ("p",)), ("t", ("g", "p"))]
    assert graph.topo_order == ["g", "t"]
    assert graph.children_of == {"p": ["g", "t"], "g": ["t"]}
    result = run_inference(graph, {"p": "x"}, {"p": 0.9})
    assert result.derived["g"].value == "from:p"
    assert result.derived["t"].value == "from:g,p"
    assert result.derived["t"].confidence == 0.9


@pytest.mark.usefixtures("probe_rule")
def test_d12_graph_and_condition_rules_keep_distinct_provenance() -> None:
    rules = [
        {
            "rule_id": "cond",
            "conditions": [{"field": "p", "operator": "EQUALS", "value": "x"}],
            "outputs": [{"field": "k", "value_expr": "K", "derivation_type": "classification"}],
            "confidence": 0.8,
        }
    ]
    bridge = InferenceBridge(rules=rules, domain_spec=_legacy({"g": _edge("p")}))
    result = bridge.run({"p": "x"}, {"p": 0.9})
    assert result.derived_fields == {"g": "from:p", "k": "K"}
    assert set(result.provenance) == {"k"}
    assert result.provenance["k"]["rule_id"] == "cond"
    traced = {entry["produced"][0]: entry["rule"] for entry in result.rule_trace}
    assert traced == {"g": "probe", "k": "cond"}


# ── D11 / F23 (legacy dict shape): one target declared on two nodes ──────────


def _two_nodes(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    return {
        "ontology": {"nodes": {"Plant": {"properties": first}, "Depot": {"properties": second}}}
    }


@pytest.mark.usefixtures("probe_rule")
def test_d11_same_target_on_two_nodes_is_blocked_not_overwritten() -> None:
    """At aabf855 the Depot edge silently replaced the Plant edge in edge_map."""
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        graph = build_derivation_graph(_two_nodes({"t": _edge("p")}, {"t": _edge("q")}))
    assert "t" not in graph.topo_order
    assert [e["nodes"] for e in logs if e["event"] == "derivation_graph.target_conflict"] == [
        ["Plant", "Depot"]
    ]
    for facts in ({"p": 1}, {"q": 1}, {"p": 1, "q": 1}):
        result = run_inference(graph, facts, dict.fromkeys(facts, 0.9))
        assert "t" not in result.derived
        assert result.blocked["t"].status is InferenceStatus.NO_RULE
        assert result.blocked["t"].value is None


@pytest.mark.usefixtures("probe_rule")
def test_d11_conflict_does_not_disturb_unambiguous_targets() -> None:
    graph = build_derivation_graph(
        _two_nodes({"t": _edge("p"), "u": _edge("p")}, {"t": _edge("q")})
    )
    result = run_inference(graph, {"p": 1}, {"p": 0.9})
    assert result.derived["u"].status is InferenceStatus.DERIVED
    assert set(result.blocked) == {"t"}


# ── D14: a fact already set is not reported as derived ──────────────────────


@pytest.mark.usefixtures("probe_rule")
def test_d14_factual_target_is_not_reported_as_derived() -> None:
    """At aabf855 the adapter copied ALREADY_SET facts into derived_fields."""
    bridge = InferenceBridge(domain_spec=_legacy({"g": _edge("p")}))
    result = bridge.run({"p": "x", "g": "fact"}, {"p": 0.9, "g": 0.95})
    assert "g" not in result.derived_fields
    assert "g" not in result.confidence_map
    assert "g" not in result.blocked_fields


@pytest.mark.usefixtures("probe_rule")
def test_d14_low_confidence_fact_is_still_rederived() -> None:
    bridge = InferenceBridge(domain_spec=_legacy({"g": _edge("p")}))
    result = bridge.run({"p": "x", "g": "weak"}, {"p": 0.9, "g": 0.3})
    assert result.derived_fields == {"g": "from:p"}
