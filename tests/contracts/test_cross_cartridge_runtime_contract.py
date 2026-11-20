"""Cross-cartridge runtime contract: one shared runtime, two unrelated cartridges.

Claim: the entities, fields, units and inference rules a converge request works
with come from the cartridge it names, never from a branch in shared code. One
cartridge cannot prove that, so a deliberately unrelated, test-only cartridge
(`tests/fixtures/cartridges/freight-carriers`) and the live PlasticOS cartridge
run through the SAME `handle_converge` and convergence loop, parametrized.

Replaced: the provider (deterministic fixed evidence) and the side-effect sinks.
Real: request validation, domain resolution, the cartridge reader, the loop and
the inference bridge. In-process only: no live Gate round trip, published
revision or writeback is claimed.

L9_META:
  tier: 2
  domain: convergence
  authority: EIE Two-Campaign Strategy Lock v0.1.0, Campaign A lane A5
  pr_class: tier2_test
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from app.engines.domain_yaml_reader import DomainYamlReader, validate_native_spec
from app.models.schemas import EnrichResponse

REPO = Path(__file__).resolve().parents[2]
FREIGHT_ROOT = REPO / "tests/fixtures/cartridges"
MAX_PASSES = 3

# As in test_canonical_converge_runtime_contract: the handler needs the Perplexity
# SDK, which the constitution gate's environment does not install. Removal
# trigger: the gate installing the project's runtime dependencies.
requires_app_runtime = pytest.mark.skipif(
    importlib.util.find_spec("perplexity") is None,
    reason="app.engines.handlers needs the Perplexity SDK; not installed in the contract gate env",
)


@dataclass(frozen=True)
class Cartridge:
    """A cartridge, provider evidence (value, confidence), and expected (value, rule_id)."""

    domain_id: str
    root: Path
    node_label: str
    entity: dict[str, Any]
    evidence: dict[str, tuple[Any, float]]
    derived: dict[str, tuple[Any, str]]

    def payload(self, **overrides: Any) -> dict[str, Any]:
        """A converge body naming this cartridge and node explicitly."""
        body: dict[str, Any] = {
            "entity": dict(self.entity),
            "object_type": self.node_label.lower(),
            "domain_id": self.domain_id,
            "node_label": self.node_label,
            "objective": "Enrich the named entity",
            "max_variations": 3,
            "consensus_threshold": 0.7,
            "max_passes": MAX_PASSES,
        }
        body.update(overrides)
        return body

    def spec(self) -> dict[str, Any]:
        loaded = yaml.safe_load((self.root / self.domain_id / "spec.yaml").read_bytes())
        assert isinstance(loaded, dict)
        return loaded


PLASTICOS = Cartridge(
    domain_id="plasticos",
    root=REPO / "domains",
    node_label="Facility",
    entity={"id": "facility:1", "name": "Acme Recycling"},
    evidence={
        "processed_polymers": ([{"code": "HDPE"}], 0.95),
        "contamination_tolerance": (2.0, 0.90),  # percent, as the cartridge declares
        "capacity_tons_month": (500.0, 0.90),
    },
    derived={
        "material_grade": ("A", "grade-assignment-hdpe-a"),
        "tier": ("tier_1", "tier-1-high-capacity"),
    },
)

FREIGHT = Cartridge(
    domain_id="freight-carriers",
    root=FREIGHT_ROOT,
    node_label="Carrier",
    entity={"id": "carrier:1", "legal_name": "Blue Ridge Freight"},
    evidence={
        "power_units": (820, 0.93),
        "on_time_pct": (0.97, 0.90),
        "hazmat_endorsed": (True, 0.92),
        "safety_rating": ("satisfactory", 0.90),
        "avg_haul_miles": (612.0, 0.88),
    },
    derived={
        "fleet_class": ("enterprise", "fleet-class-enterprise"),
        "haul_profile": ("long_haul", "haul-profile-long"),
        "hazmat_qualified": ("qualified", "hazmat-qualified"),
        "service_tier": ("preferred", "service-tier-preferred"),
    },
)

CARTRIDGES = pytest.mark.parametrize("cartridge", [PLASTICOS, FREIGHT], ids=lambda c: c.domain_id)


def _field_names(spec: dict[str, Any]) -> set[str]:
    return {p["name"] for node in spec["ontology"]["nodes"] for p in node["properties"]}


async def _converge(
    cartridge: Cartridge,
    evidence: dict[str, tuple[Any, float]],
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the live handler and loop; return (response, kwargs the loop received)."""
    from app.engines import handlers
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
        return await real_loop(**kwargs)

    with (
        patch("app.engines.handlers._domain_reader", DomainYamlReader(cartridge.root)),
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
            result = await handlers.handle_converge("tenant-a", payload)
        finally:
            get_side_effect_coordinator().reset_for_tests()
    return result, seen


def _provenance(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for entry in result["inferences"]:
        merged.update(entry.get("inference_provenance", {}))
    return merged


@pytest.mark.unit
def test_freight_cartridge_is_a_valid_native_spec() -> None:
    """The fixture passes the same native validation a published tenant does."""
    spec = FREIGHT.spec()
    assert validate_native_spec(spec, FREIGHT.domain_id) == spec


@pytest.mark.unit
def test_cartridges_share_no_entity_field_or_rule() -> None:
    """Different entities, fields and rules, so passing proves nothing by overlap."""
    plasticos, freight = PLASTICOS.spec(), FREIGHT.spec()

    def labels(spec: dict[str, Any]) -> set[str]:
        return {node["label"] for node in spec["ontology"]["nodes"]}

    def rules(spec: dict[str, Any], key: str) -> set[str]:
        if key == "rule_id":
            return {rule[key] for rule in spec["inference_rules"]}
        return {c[key] for rule in spec["inference_rules"] for c in rule["conditions"]}

    assert labels(plasticos).isdisjoint(labels(freight))
    assert _field_names(plasticos).isdisjoint(_field_names(freight))
    assert rules(plasticos, "rule_id").isdisjoint(rules(freight, "rule_id"))
    # The freight rules also exercise operators PlasticOS never uses.
    new_operators = rules(freight, "operator") - rules(plasticos, "operator")
    assert new_operators >= {"ISTRUE", "NOTIN", "GT", "EQUALS"}


@pytest.mark.unit
def test_freight_cartridge_is_test_only() -> None:
    """Not a tenant: absent from `domains/`, and unknown to shared code."""
    assert not (REPO / "domains" / FREIGHT.domain_id).exists()
    mentions = [
        str(path.relative_to(REPO))
        for path in sorted((REPO / "app").rglob("*.py"))
        if "freight" in path.read_text(encoding="utf-8").lower()
    ]
    assert mentions == [], f"shared code must not know the test cartridge: {mentions}"


@requires_app_runtime
@CARTRIDGES
async def test_named_cartridge_directs_a_bounded_run(cartridge: Cartridge) -> None:
    """The loop gets the named node's hints and the cartridge's rules, and converges."""
    result, seen = await _converge(cartridge, cartridge.evidence, cartridge.payload())

    spec = cartridge.spec()
    node = next(n for n in spec["ontology"]["nodes"] if n["label"] == cartridge.node_label)
    for key in ("objective_template", "priority_fields", "min_confidence"):
        assert seen["domain_hints"][key] == node["enrichment_hints"][key]
    assert seen["inference_rules"] == spec["inference_rules"]

    assert EnrichResponse.model_validate(result).model_dump() == result
    assert (result["state"], result["failure_reason"]) == ("completed", None)
    assert 1 <= result["pass_count"] <= MAX_PASSES
    assert result["convergence_reason"]
    for name, (value, _conf) in cartridge.evidence.items():
        assert result["fields"][name] == value


@requires_app_runtime
@CARTRIDGES
async def test_derived_fields_come_from_the_cartridges_own_rules(cartridge: Cartridge) -> None:
    """Every derived value names its rule and version, and never gains confidence."""
    result, _seen = await _converge(cartridge, cartridge.evidence, cartridge.payload())

    provenance = _provenance(result)
    assert set(provenance) == set(cartridge.derived)
    rule_versions = {
        rule["rule_id"]: rule["rule_or_model_version"]
        for rule in cartridge.spec()["inference_rules"]
    }
    for name, (value, rule_id) in cartridge.derived.items():
        record = provenance[name]
        assert result["fields"][name] == value
        assert record["rule_id"] == rule_id
        # The version the cartridge itself declares for that rule.
        assert record["rule_version"] == rule_versions[rule_id]
        assert set(record["inputs"]) <= _field_names(cartridge.spec())
        assert record["confidence"] <= min(record["input_confidence"].values())
        assert record["confidence"] <= record["rule_confidence"]
    assert result["feature_vector"]["inference"]["rule_versions"] == sorted(
        set(rule_versions.values())
    )

    other = FREIGHT if cartridge is PLASTICOS else PLASTICOS
    assert set(result["fields"]).isdisjoint(_field_names(other.spec()))


@requires_app_runtime
@pytest.mark.parametrize(
    ("cartridge", "foreign"),
    [(PLASTICOS, FREIGHT), (FREIGHT, PLASTICOS)],
    ids=["plasticos", "freight"],
)
async def test_another_cartridges_evidence_derives_nothing(
    cartridge: Cartridge, foreign: Cartridge
) -> None:
    """Rules fire on the loaded cartridge's fields only: no cross-cartridge leak."""
    result, _seen = await _converge(cartridge, foreign.evidence, cartridge.payload())

    assert _provenance(result) == {}
    assert set(result["fields"]).isdisjoint(cartridge.derived)
    assert set(result["fields"]).isdisjoint(foreign.derived)


@requires_app_runtime
async def test_unmet_conditions_derive_nothing() -> None:
    """A rule whose condition is not met stays silent; nothing is filled in."""
    evidence = {
        **FREIGHT.evidence,
        "safety_rating": ("conditional", 0.90),
        "power_units": (12, 0.93),
    }

    result, _seen = await _converge(FREIGHT, evidence, FREIGHT.payload())

    assert set(_provenance(result)) == {"haul_profile"}
    for absent in ("fleet_class", "hazmat_qualified", "service_tier"):
        assert absent not in result["fields"]


@requires_app_runtime
async def test_multi_node_cartridge_has_no_implicit_node() -> None:
    """With no `node_label`, a two-node cartridge yields no hints; rules still load."""
    payload = FREIGHT.payload()
    del payload["node_label"]

    result, seen = await _converge(FREIGHT, FREIGHT.evidence, payload)

    assert seen["domain_hints"] == {}
    assert seen["inference_rules"] == FREIGHT.spec()["inference_rules"]
    assert result["state"] == "completed"
