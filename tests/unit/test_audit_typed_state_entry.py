"""EIE-PR-U01: every value entering convergence state satisfies its declared type.

L9_META:
  tier: 2
  domain: typed-evidence
  authority: operator ruling 2026-11-27 (EIE-PR-U01): "Every value entering
    authoritative convergence state under a declared property must satisfy that
    property's type contract, regardless of its origin.";
    app/engines/spec_types.py (the one typed-declaration grammar);
    docs/contracts/data/graph_return_intake.md (consumption refusals)

Provider replies are checked at admission (`validation_engine.validate_response`).
Two other origins wrote into `ConvergenceState` unchecked:

* a graph-return target (`_apply_graph_targets`) was refused only for an
  undeclared field, so a declared `float` could be seeded with `"lots"`;
* an inference derivation was stored as-is, so a rule could write a value
  outside its field's enum.

Owners exercised: ``convergence_controller._apply_graph_targets`` and
``run_convergence_loop`` (its inference merge). The expected verdict for every
value is `spec_types.value_defects`, the grammar the graph row enforces.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.config import Settings
from app.engines import convergence_controller
from app.engines.convergence.confidence_tracker import ConfidenceTracker
from app.engines.convergence_controller import (
    ConvergenceState,
    GraphConsumption,
    _apply_graph_targets,
    _field_declarations,
)
from app.engines.spec_types import value_defects
from app.models.schemas import EnrichRequest, EnrichResponse
from app.services.graph_return_channel import REJECT_TYPE_MISMATCH, EnrichmentTarget

pytestmark = pytest.mark.unit

GRADE = {"name": "grade", "type": "enum", "values": ["A", "B"]}
CAPACITY = {"name": "capacity", "type": "float"}
SINCE = {"name": "since", "type": "date"}
DECLARATIONS = {d["name"]: (d,) for d in (GRADE, CAPACITY, SINCE)}
DECLARED = frozenset(DECLARATIONS)


def _target(field: str, value: Any, confidence: float = 0.9) -> EnrichmentTarget:
    return EnrichmentTarget(
        entity_id="d-1",
        tenant_id="synthco",
        field_name=field,
        seed_value=value,
        source_confidence=confidence,
        origin_packet_id="pkt-1",
        origin_inference_rule="r1",
        intake_id=str(uuid.uuid4()),
    )


def _seed(targets: list[EnrichmentTarget]) -> tuple[dict[str, Any], ConvergenceState, Any]:
    state = ConvergenceState()
    ledger = GraphConsumption()
    seeded = _apply_graph_targets(
        targets,
        state,
        ConfidenceTracker(),
        1,
        declared_fields=DECLARED,
        declarations=DECLARATIONS,
        ledger=ledger,
    )
    return seeded, state, ledger


# ── Graph-return targets ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capacity", "lots"),
        ("capacity", True),
        ("capacity", {"t": 2}),
        ("grade", "C"),
        ("grade", 1),
        ("since", "yesterday"),
        ("since", 20261127),
    ],
)
def test_a_seed_that_violates_its_declared_type_is_refused(field: str, value: Any) -> None:
    target = _target(field, value)
    seeded, state, ledger = _seed([target])

    assert seeded == {}
    assert field not in state.known_fields and field not in state.inferred_fields
    assert field not in state.confidence_map
    assert ledger.rejections == {target.intake_id: REJECT_TYPE_MISMATCH}
    assert target.intake_id not in ledger.outcomes


@pytest.mark.parametrize(
    ("field", "value"),
    [("capacity", 2.5), ("capacity", 3), ("grade", "A"), ("since", "2026-11-27")],
)
def test_a_seed_that_conforms_is_seeded_unchanged(field: str, value: Any) -> None:
    target = _target(field, value)
    seeded, state, ledger = _seed([target])

    assert seeded == {field: value}
    assert state.known_fields[field] == value
    assert ledger.outcomes == {target.intake_id: "seeded"}
    assert ledger.rejections == {}


def test_a_refused_seed_never_displaces_a_conforming_one() -> None:
    good, bad = _target("grade", "A", 0.7), _target("grade", "Z", 0.99)
    seeded, state, ledger = _seed([good, bad])

    assert seeded == {"grade": "A"} and state.known_fields["grade"] == "A"
    assert ledger.outcomes == {good.intake_id: "seeded"}
    assert ledger.rejections == {bad.intake_id: REJECT_TYPE_MISMATCH}


def test_an_undeclared_field_keeps_its_own_refusal_reason() -> None:
    target = _target("shoe_size", "nine")
    _, _, ledger = _seed([target])
    assert ledger.rejections == {target.intake_id: "field_not_in_schema"}


# ── Declarations: which contract a field's value is held to ─────────────────

TWO_NODES = {
    "ontology": {
        "nodes": [
            {"label": "Carrier", "properties": [{"name": "size", "type": "int"}]},
            {"label": "Lane", "properties": [{"name": "size", "type": "enum", "values": ["S"]}]},
        ]
    }
}


def test_a_bound_node_supplies_only_its_own_declarations() -> None:
    declarations = _field_declarations(TWO_NODES, "Carrier")
    assert declarations == {"size": ({"name": "size", "type": "int"},)}


def test_an_unbound_value_must_satisfy_every_declaration_of_its_name() -> None:
    """No node named: the property is ambiguous, so no declaration of it may be violated."""
    declarations = _field_declarations(TWO_NODES, None)
    assert declarations is not None and len(declarations["size"]) == 2
    for value in (3, "S"):  # each satisfies one node's declaration only
        target = _target("size", value)
        state, ledger = ConvergenceState(), GraphConsumption()
        _apply_graph_targets(
            [target], state, ConfidenceTracker(), 1, declarations=declarations, ledger=ledger
        )
        assert ledger.rejections == {target.intake_id: REJECT_TYPE_MISMATCH}, value


@pytest.mark.parametrize("spec", [None, {}, {"ontology": {"nodes": []}}])
def test_no_declared_ontology_checks_nothing(spec: Any) -> None:
    assert _field_declarations(spec, None) is None


# ── Inference derivations ───────────────────────────────────────────────────

SPEC = {
    "ontology": {
        "nodes": [
            {
                "label": "Carrier",
                "properties": [
                    {"name": "power_units", "type": "int"},
                    {"name": "fleet_class", "type": "enum", "values": ["enterprise", "regional"]},
                ],
            }
        ]
    }
}


def _rule(value: Any) -> dict[str, Any]:
    return {
        "rule_id": "fleet-class",
        "node": "Carrier",
        "conditions": [{"field": "power_units", "operator": "GTE", "value": 500}],
        "outputs": [
            {"field": "fleet_class", "value_expr": value, "derivation_type": "classification"}
        ],
        "confidence": 0.95,
        "priority": 20,
        "rule_or_model_version": "t-0.1.0",
    }


async def _converge(rule_value: Any) -> EnrichResponse:
    async def enricher(*_args: Any, **_kwargs: Any) -> EnrichResponse:
        return EnrichResponse(
            state="completed", fields={"power_units": 820}, confidence=0.93, tokens_used=1
        )

    request = EnrichRequest(entity={"id": "c-1"}, object_type="carrier", objective="probe")
    return await convergence_controller.run_convergence_loop(
        request=request,
        settings=Settings(l9_environment="test"),
        kb_resolver=None,
        enricher=enricher,
        inference_rules=[_rule(rule_value)],
        domain_spec=SPEC,
        node_label="Carrier",
    )


@pytest.mark.asyncio
async def test_a_derivation_outside_its_declared_enum_never_reaches_the_answer() -> None:
    response = await _converge("galactic")

    assert "fleet_class" not in response.fields
    # Nowhere in the answer: not a field, an inference record or replay payload.
    assert "galactic" not in str(response.model_dump())


@pytest.mark.asyncio
async def test_control_a_conforming_derivation_is_kept() -> None:
    response = await _converge("enterprise")

    assert response.fields.get("fleet_class") == "enterprise"


# ── The verdict is the row grammar's ────────────────────────────────────────


@pytest.mark.parametrize(
    ("decl", "value"),
    [
        (GRADE, "A"),
        (GRADE, "C"),
        (CAPACITY, 1.0),
        (CAPACITY, "x"),
        (SINCE, "2026-11-27"),
        (SINCE, "x"),
    ],
)
def test_seeding_and_the_row_grammar_reach_the_same_verdict(
    decl: dict[str, Any], value: Any
) -> None:
    target = _target(decl["name"], value)
    _, _, ledger = _seed([target])
    conforms = not value_defects(value, decl, decl["name"])
    assert (target.intake_id not in ledger.rejections) is conforms


@pytest.mark.asyncio
async def test_an_off_type_provider_value_from_any_enricher_never_reaches_state() -> None:
    """Admission checks replies; the merge re-checks so an injected enricher cannot bypass it."""

    async def enricher(*_args: Any, **_kwargs: Any) -> EnrichResponse:
        return EnrichResponse(
            state="completed", fields={"power_units": "many"}, confidence=0.93, tokens_used=1
        )

    request = EnrichRequest(entity={"id": "c-1"}, object_type="carrier", objective="probe")
    response = await convergence_controller.run_convergence_loop(
        request=request,
        settings=Settings(l9_environment="test"),
        kb_resolver=None,
        enricher=enricher,
        domain_spec=SPEC,
        node_label="Carrier",
    )
    assert "power_units" not in response.fields


# ── A refused derivation takes its descendants with it (review: Codex P1) ──

CASCADE_SPEC = {
    "ontology": {
        "nodes": [
            {
                "label": "Carrier",
                "properties": [
                    {"name": "power_units", "type": "int"},
                    {"name": "fleet_class", "type": "enum", "values": ["enterprise", "regional"]},
                    {"name": "tier", "type": "enum", "values": ["gold", "silver"]},
                ],
            }
        ]
    }
}


def _cascade_rules(fleet_class: str) -> list[dict[str, Any]]:
    tier = {
        "rule_id": "tier-from-class",
        "node": "Carrier",
        "conditions": [{"field": "fleet_class", "operator": "EQUALS", "value": fleet_class}],
        "outputs": [{"field": "tier", "value_expr": "gold", "derivation_type": "classification"}],
        "confidence": 0.9,
        "priority": 10,
        "rule_or_model_version": "t-0.1.0",
    }
    return [_rule(fleet_class), tier]


async def _converge_cascade(fleet_class: str) -> EnrichResponse:
    async def enricher(*_args: Any, **_kwargs: Any) -> EnrichResponse:
        return EnrichResponse(
            state="completed", fields={"power_units": 820}, confidence=0.93, tokens_used=1
        )

    request = EnrichRequest(entity={"id": "c-1"}, object_type="carrier", objective="probe")
    return await convergence_controller.run_convergence_loop(
        request=request,
        settings=Settings(l9_environment="test"),
        kb_resolver=None,
        enricher=enricher,
        inference_rules=_cascade_rules(fleet_class),
        domain_spec=CASCADE_SPEC,
        node_label="Carrier",
    )


@pytest.mark.asyncio
async def test_a_valid_derivation_built_on_a_refused_one_is_refused_too() -> None:
    """`galactic` is refused; `tier=gold` was derived from it, so it goes as well."""
    response = await _converge_cascade("galactic")

    assert "fleet_class" not in response.fields
    assert "tier" not in response.fields
    assert "galactic" not in str(response.model_dump())


@pytest.mark.asyncio
async def test_control_a_cascade_over_a_conforming_derivation_is_kept() -> None:
    response = await _converge_cascade("enterprise")

    assert response.fields.get("fleet_class") == "enterprise"
    assert response.fields.get("tier") == "gold"
