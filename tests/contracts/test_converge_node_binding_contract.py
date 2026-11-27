"""F06 caller wiring: a converge request runs only its own node's inference rules.

`InferenceBridge(node=...)` binds inference to one ontology node (#400). This
pins the caller half: `handle_converge` resolves the node the request addresses
and `run_convergence_loop` hands it to the bridge.

- v2 request: the resolved ``node``; v1 request: its ``node_label``, else the
  spec's only node; a multi-node spec that names none passes no node and runs
  every rule, as before.
- Multi-node cartridge (tests/fixtures/cartridges/freight-carriers, plus one
  in-memory Lane rule so the reverse direction is provable): Carrier rules never
  run on a Lane request and the Lane rule never runs on a Carrier request, even
  when the evidence carries the other node's fields.
- Single-node cartridge (domains/plasticos): the answer is identical with the
  node wired and with no node, every key but wall-clock timing.
- A cartridge with no rules: identical with and without the node.

Real: request resolution, the cartridge reader, the handler, the loop and the
bridge. Replaced: the provider (fixed evidence) and the side-effect sinks.

L9_META:
  tier: 2
  domain: convergence
  authority: CTO ASSIGN EIE-W-F06-WIRING (F06 caller wiring, #400 follow-up)
  pr_class: tier2_test
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from app.models.schemas import EnrichResponse

REPO = Path(__file__).resolve().parents[2]
FREIGHT_SPEC = REPO / "tests/fixtures/cartridges/freight-carriers/spec.yaml"
PLASTICOS_ROOT = REPO / "domains"
MAX_PASSES = 3

# As in test_cross_cartridge_runtime_contract: the handler needs the provider SDK,
# which the constitution gate's environment does not install. Removal trigger:
# the gate installing the project's runtime dependencies.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        importlib.util.find_spec("perplexity") is None,
        reason="app.engines.handlers needs the Perplexity SDK; not installed in the contract gate env",
    ),
]

CARRIER_EVIDENCE: dict[str, tuple[Any, float]] = {
    "power_units": (820, 0.93),
    "on_time_pct": (0.97, 0.90),
    "hazmat_endorsed": (True, 0.92),
    "safety_rating": ("satisfactory", 0.90),
    "avg_haul_miles": (612.0, 0.88),
}
CARRIER_DERIVED = {"fleet_class", "haul_profile", "hazmat_qualified", "service_tier"}
LANE_EVIDENCE: dict[str, tuple[Any, float]] = {
    "distance_miles": (1450.0, 0.92),
    "transit_hours": (30.0, 0.90),
}
LANE_RULE: dict[str, Any] = {
    "rule_id": "lane-class-long",
    "node": "Lane",
    "conditions": [{"field": "distance_miles", "operator": "GT", "value": 1000}],
    "outputs": [{"field": "lane_class", "value_expr": "long", "derivation_type": "classification"}],
    "confidence": 0.85,
    "priority": 10,
    "rule_or_model_version": "freight-lane-probe-0.1.0",
}
PLASTICOS_EVIDENCE: dict[str, tuple[Any, float]] = {
    "processed_polymers": ([{"code": "HDPE"}], 0.95),
    "contamination_tolerance": (2.0, 0.90),
    "capacity_tons_month": (500.0, 0.90),
}
# Wall-clock only; every other key of the answer must match exactly.
VOLATILE = {"processing_time_ms"}


def _freight(*, lane_rule: bool = True, rules: bool = True) -> dict[str, Any]:
    """The checked-in freight cartridge, optionally with a Lane rule or with no rules."""
    spec = yaml.safe_load(FREIGHT_SPEC.read_bytes())
    assert isinstance(spec, dict)
    spec = copy.deepcopy(spec)
    if lane_rule:
        lane = next(n for n in spec["ontology"]["nodes"] if n["label"] == "Lane")
        lane["properties"].append({"name": "lane_class", "type": "string"})
        spec["inference_rules"].append(dict(LANE_RULE))
    if not rules:
        del spec["inference_rules"]
    return spec


def _write(root: Path, spec: dict[str, Any]) -> Path:
    folder = root / str(spec["name"])
    folder.mkdir(parents=True)
    (folder / "spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return root


async def _converge(
    root: Path,
    tenant: str,
    payload: dict[str, Any],
    evidence: dict[str, tuple[Any, float]],
    *,
    drop_node: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the live handler and loop; return (response, kwargs the loop received).

    ``drop_node`` reproduces the loop before F06 wiring: the node never reaches
    the bridge.
    """
    from app.engines import handlers
    from app.engines.domain_yaml_reader import DomainYamlReader
    from app.services.side_effect_coordinator import get_side_effect_coordinator

    async def provider(request: Any, settings: Any, *_args: Any, **_kw: Any) -> EnrichResponse:
        return EnrichResponse(
            state="completed",
            fields={name: value for name, (value, _conf) in evidence.items()},
            confidence=min(conf for _value, conf in evidence.values()),
            tokens_used=100,
            feature_vector={
                "per_field_confidence": {name: conf for name, (_value, conf) in evidence.items()}
            },
        )

    seen: dict[str, Any] = {}
    real_loop = handlers.run_convergence_loop

    async def spy_loop(**kwargs: Any) -> EnrichResponse:
        seen.update(kwargs)
        if drop_node:
            kwargs.pop("node_label", None)
        return await real_loop(**kwargs)

    with (
        patch("app.engines.handlers._domain_reader", DomainYamlReader(root)),
        patch("app.engines.handlers.run_convergence_loop", spy_loop),
        patch("app.engines.enrichment_orchestrator.enrich_entity", AsyncMock(side_effect=provider)),
        patch("app.services.result_store.ResultStore.persist_enrich_response"),
        patch("app.engines.packet_router.get_router") as router_factory,
        patch("app.services.event_emitter.get_emitter") as emitter_factory,
    ):
        router_factory.return_value.notify_graph_sync = AsyncMock()
        router_factory.return_value.notify_score_invalidate = AsyncMock()
        emitter_factory.return_value.emit_enrichment_completed = AsyncMock()
        get_side_effect_coordinator().reset_for_tests()
        try:
            result = await handlers.handle_converge(tenant, payload)
        finally:
            get_side_effect_coordinator().reset_for_tests()
    return result, seen


def _v1(domain_id: str, node_label: str | None, entity_id: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "entity": {"id": entity_id},
        "object_type": (node_label or domain_id).lower(),
        "domain_id": domain_id,
        "objective": "Enrich the named entity",
        "max_variations": 3,
        "consensus_threshold": 0.7,
        "max_passes": MAX_PASSES,
    }
    if node_label is not None:
        body["node_label"] = node_label
    return body


def _derived(result: dict[str, Any]) -> dict[str, str]:
    """Derived field -> the rule that derived it."""
    merged: dict[str, str] = {}
    for entry in result["inferences"]:
        for name, record in entry.get("inference_provenance", {}).items():
            merged[name] = record["rule_id"]
    return merged


def _stable(result: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key not in VOLATILE}


async def test_a_lane_request_runs_no_carrier_rule(tmp_path: Path) -> None:
    """Carrier evidence on a Lane request derives nothing: the Carrier rules never run."""
    root = _write(tmp_path, _freight())
    evidence = {**CARRIER_EVIDENCE, **LANE_EVIDENCE}
    result, seen = await _converge(
        root, "tenant-a", _v1("freight-carriers", "Lane", "lane:1"), evidence
    )

    assert seen["node_label"] == "Lane"
    assert result["state"] == "completed"
    assert _derived(result) == {"lane_class": "lane-class-long"}
    assert set(result["fields"]).isdisjoint(CARRIER_DERIVED)
    assert result["feature_vector"]["inference"]["rule_count"] == 1

    # Control: the same request before the wiring runs the Carrier rules too.
    before, _ = await _converge(
        root, "tenant-a", _v1("freight-carriers", "Lane", "lane:1"), evidence, drop_node=True
    )
    assert set(_derived(before)) == CARRIER_DERIVED | {"lane_class"}


async def test_a_carrier_request_runs_no_lane_rule(tmp_path: Path) -> None:
    """Lane evidence on a Carrier request never reaches the Lane rule."""
    root = _write(tmp_path, _freight())
    evidence = {**CARRIER_EVIDENCE, **LANE_EVIDENCE}
    result, seen = await _converge(
        root, "tenant-a", _v1("freight-carriers", "Carrier", "carrier:1"), evidence
    )

    assert seen["node_label"] == "Carrier"
    assert set(_derived(result)) == CARRIER_DERIVED
    assert "lane_class" not in result["fields"]
    assert result["feature_vector"]["inference"]["rule_count"] == 5


async def test_a_v2_request_binds_its_resolved_node(tmp_path: Path) -> None:
    """A v2 request's resolved `node` is the node inference is bound to."""
    spec = _freight()
    root = _write(tmp_path, spec)
    payload = {
        "contract": "eie.converge.request/v2",
        "cartridge": {"id": "freight-carriers", "version": spec["version"]},
        "node": "Lane",
        "targets": [{"field": "distance_miles"}, {"field": "lane_class"}],
        "entity": {"id": "lane:2"},
        "object_type": "lane",
        "objective": "Enrich the lane",
        "max_variations": 3,
        "consensus_threshold": 0.7,
        "idempotency_key": "f06:v2:lane",
    }
    result, seen = await _converge(
        root, "freight-carriers", payload, {**CARRIER_EVIDENCE, **LANE_EVIDENCE}
    )

    assert seen["node_label"] == "Lane"
    assert (result["state"], result["failure_reason"]) == ("completed", None)
    assert _derived(result) == {"lane_class": "lane-class-long"}
    assert result["feature_vector"]["inference"]["rule_count"] == 1


async def test_a_multi_node_request_naming_no_node_runs_every_rule(tmp_path: Path) -> None:
    """No implicit node in a two-node cartridge: nothing is bound, as before F06."""
    root = _write(tmp_path, _freight())
    evidence = {**CARRIER_EVIDENCE, **LANE_EVIDENCE}
    payload = _v1("freight-carriers", None, "carrier:3")
    result, seen = await _converge(root, "tenant-a", payload, evidence)
    before, _ = await _converge(root, "tenant-a", payload, evidence, drop_node=True)

    assert seen["node_label"] is None
    assert set(_derived(result)) == CARRIER_DERIVED | {"lane_class"}
    assert _stable(result) == _stable(before)


@pytest.mark.parametrize("node_label", ["Facility", None], ids=["named", "spec-default"])
async def test_a_single_node_cartridge_answers_identically(node_label: str | None) -> None:
    """PlasticOS: the wired node changes nothing in the answer."""
    payload = _v1("plasticos", node_label, "facility:1")
    wired, seen = await _converge(PLASTICOS_ROOT, "tenant-a", payload, PLASTICOS_EVIDENCE)
    before, _ = await _converge(
        PLASTICOS_ROOT, "tenant-a", payload, PLASTICOS_EVIDENCE, drop_node=True
    )

    assert seen["node_label"] == "Facility"
    assert set(_derived(wired)) == {"material_grade", "tier"}
    assert _stable(wired) == _stable(before)


async def test_a_cartridge_with_no_rules_answers_identically(tmp_path: Path) -> None:
    root = _write(tmp_path, _freight(lane_rule=False, rules=False))
    payload = _v1("freight-carriers", "Carrier", "carrier:4")
    wired, seen = await _converge(root, "tenant-a", payload, CARRIER_EVIDENCE)
    before, _ = await _converge(root, "tenant-a", payload, CARRIER_EVIDENCE, drop_node=True)

    assert seen["node_label"] == "Carrier"
    assert _derived(wired) == {}
    assert wired["feature_vector"]["inference"]["status"] == "not_configured"
    assert _stable(wired) == _stable(before)
