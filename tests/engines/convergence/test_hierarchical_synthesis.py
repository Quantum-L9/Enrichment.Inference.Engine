"""
test_hierarchical_synthesis.py — 17 deterministic test cases
"""

from app.engines.convergence.convergence_config import ConvergenceConfig
from app.engines.convergence.hierarchical_synthesis import (
    _COMPOSITE_NODES_KEY,
    _MAX_FIELDS_PER_NODE,
    _MIN_COMPOSITE_CONFIDENCE,
    CompositeNode,
    attach_composites_to_feature_vector,
    build_composite_nodes,
    synthesize_and_attach,
)
from app.models.enrichment import ConvergenceState, EnrichResponse, FieldResult


def _cfg(enabled: bool = True, derived_targets: list[str] | None = None) -> ConvergenceConfig:
    cfg = ConvergenceConfig(synthesize_composites=enabled)
    cfg.derived_field_targets = derived_targets or []
    return cfg


def _field_result(confidence: float = 0.85, resolved_pass: int = 2) -> FieldResult:
    fr = FieldResult(field_name="field", value=None, confidence=confidence)
    fr.resolved_pass = resolved_pass
    return fr


def _state(resolved: dict | None = None) -> ConvergenceState:
    state = ConvergenceState()
    state.resolved_fields = resolved or {}
    return state


def _response(fv: dict | None = None) -> EnrichResponse:
    return EnrichResponse(feature_vector=fv if fv is not None else {})


# ── Feature flag ──────────────────────────────────────────────────────────────


def test_disabled_returns_empty():
    state = _state({"a": _field_result(), "b": _field_result()})
    nodes = build_composite_nodes(state, _cfg(enabled=False))
    assert nodes == []


def test_disabled_response_unchanged():
    result = synthesize_and_attach(_state(), _response({"x": 1}), _cfg(enabled=False))
    assert result.feature_vector == {"x": 1}


# ── Confidence filtering ──────────────────────────────────────────────────────


def test_low_confidence_excluded():
    state = _state(
        {
            "low": _field_result(confidence=0.5, resolved_pass=2),
            "high": _field_result(confidence=0.90, resolved_pass=2),
            "also_high": _field_result(confidence=0.80, resolved_pass=2),
        }
    )
    nodes = build_composite_nodes(state, _cfg())
    all_fields = [f for n in nodes for f in n.constituent_fields]
    assert "low" not in all_fields
    assert "high" in all_fields


def test_exactly_at_threshold_included():
    state = _state(
        {
            "a": _field_result(confidence=_MIN_COMPOSITE_CONFIDENCE, resolved_pass=3),
            "b": _field_result(confidence=_MIN_COMPOSITE_CONFIDENCE, resolved_pass=3),
        }
    )
    nodes = build_composite_nodes(state, _cfg())
    assert len(nodes) >= 1


# ── Node construction ─────────────────────────────────────────────────────────


def test_node_max_fields_enforced():
    state = _state({f"f{i}": _field_result(resolved_pass=2) for i in range(12)})
    nodes = build_composite_nodes(state, _cfg())
    for n in nodes:
        assert len(n.constituent_fields) <= _MAX_FIELDS_PER_NODE


def test_single_field_no_node():
    state = _state({"solo": _field_result(resolved_pass=2)})
    nodes = build_composite_nodes(state, _cfg())
    assert nodes == []


def test_different_passes_separate_nodes():
    state = _state(
        {
            "a": _field_result(resolved_pass=2),
            "b": _field_result(resolved_pass=2),
            "c": _field_result(resolved_pass=3),
            "d": _field_result(resolved_pass=3),
        }
    )
    nodes = build_composite_nodes(state, _cfg())
    passes = {n.resolved_pass for n in nodes}
    assert 2 in passes and 3 in passes


def test_derived_targets_excluded():
    state = _state(
        {
            "raw_field": _field_result(resolved_pass=2),
            "derived_field": _field_result(resolved_pass=2),
            "raw_field2": _field_result(resolved_pass=2),
        }
    )
    nodes = build_composite_nodes(state, _cfg(derived_targets=["derived_field"]))
    all_fields = [f for n in nodes for f in n.constituent_fields]
    assert "derived_field" not in all_fields


def test_node_id_deterministic():
    n1 = CompositeNode.build(["a", "b", "c"], 2, 0.85, False)
    n2 = CompositeNode.build(["c", "a", "b"], 2, 0.85, False)
    assert n1.node_id == n2.node_id


# ── Feature vector attachment ─────────────────────────────────────────────────


def test_attach_adds_composite_key():
    nodes = [CompositeNode.build(["a", "b"], 2, 0.85, False)]
    response = _response({})
    result = attach_composites_to_feature_vector(response, nodes)
    assert result.feature_vector is not None
    assert _COMPOSITE_NODES_KEY in result.feature_vector


def test_attach_empty_nodes_unchanged():
    response = _response({"existing": 1})
    result = attach_composites_to_feature_vector(response, [])
    assert result.feature_vector is not None
    assert _COMPOSITE_NODES_KEY not in result.feature_vector
    assert result.feature_vector["existing"] == 1


def test_attach_preserves_existing_keys():
    nodes = [CompositeNode.build(["a", "b"], 2, 0.85, False)]
    response = _response({"prior_key": "value"})
    result = attach_composites_to_feature_vector(response, nodes)
    assert result.feature_vector is not None
    assert result.feature_vector["prior_key"] == "value"
    assert _COMPOSITE_NODES_KEY in result.feature_vector


def test_node_feature_entry_structure():
    n = CompositeNode.build(["hdpe_grade", "mfi_range"], 2, 0.88, False)
    entry = n.to_feature_entry()
    assert "node_id" in entry
    assert "pass" in entry
    assert "fields" in entry
    assert "confidence" in entry
    assert "label" in entry


def test_no_empty_state_panic():
    result = build_composite_nodes(_state({}), _cfg())
    assert result == []
