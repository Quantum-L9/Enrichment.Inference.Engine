"""
test_corrective_retrieval.py — 15 deterministic test cases
"""

from dataclasses import dataclass, field

from app.engines.convergence.convergence_config import ConvergenceConfig
from app.engines.convergence.corrective_retrieval import (
    _CORRECTIVE_TARGETS_KEY,
    CorrectiveState,
    CorrectiveTarget,
    apply_corrective_override,
    consume_corrective_override,
    extract_corrective_targets,
    should_apply_corrective,
)
from app.models.common import FieldStatus, FieldTrace
from app.models.enrichment import ConvergenceState


@dataclass
class _InferenceSnapshot:
    """Test double with the blocked_fields / rule_trace attrs extract() reads."""

    blocked_fields: dict
    rule_trace: dict = field(default_factory=dict)


def _cfg(enabled: bool = True, threshold: float = 0.75) -> ConvergenceConfig:
    return ConvergenceConfig(
        corrective_retrieval_enabled=enabled,
        confidence_threshold=threshold,
    )


def _inference(blocked: dict, rule_trace: dict | None = None) -> _InferenceSnapshot:
    return _InferenceSnapshot(blocked_fields=blocked, rule_trace=rule_trace or {})


def _state(meta: dict | None = None) -> ConvergenceState:
    return ConvergenceState(metadata=meta or {})


def _trace(
    status: FieldStatus | str,
    unlock_map: dict | None = None,
    missing: list | None = None,
    confidence: float = 0.5,
    field_name: str = "field",
) -> FieldTrace:
    return FieldTrace(
        field_name=field_name,
        status=status if isinstance(status, FieldStatus) else FieldStatus(status),
        confidence=confidence,
        extra={
            "unlock_map": unlock_map or {},
            "missing_inputs": missing or [],
        },
    )


# ── should_apply_corrective ───────────────────────────────────────────────────


def test_gate_blocks_pass_1():
    assert should_apply_corrective(1, _cfg()) is False


def test_gate_allows_pass_2():
    assert should_apply_corrective(2, _cfg()) is True


def test_gate_disabled_by_config():
    assert should_apply_corrective(3, _cfg(enabled=False)) is False


def test_gate_allows_high_pass():
    assert should_apply_corrective(10, _cfg()) is True


# ── extract_corrective_targets ────────────────────────────────────────────────


def test_extract_empty_blocked():
    targets = extract_corrective_targets(_inference({}), _state(), _cfg())
    assert targets == []


def test_extract_inputs_missing_ranked_first():
    blocked = {
        "material_grade": FieldStatus.INPUTS_MISSING,
        "mfi_range": FieldStatus.BELOW_THRESHOLD,
    }
    traces = {
        "material_grade": _trace(
            FieldStatus.INPUTS_MISSING,
            unlock_map={"material_grade": ["a", "b"]},
            confidence=0.4,
            field_name="material_grade",
        ),
        "mfi_range": _trace(
            FieldStatus.BELOW_THRESHOLD,
            unlock_map={"mfi_range": ["x", "y", "z"]},
            confidence=0.6,
            field_name="mfi_range",
        ),
    }
    targets = extract_corrective_targets(_inference(blocked, traces), _state(), _cfg())
    assert targets[0].field_name == "material_grade"


def test_extract_unlock_score_ordering():
    blocked = {"a": FieldStatus.BELOW_THRESHOLD, "b": FieldStatus.BELOW_THRESHOLD}
    traces = {
        "a": _trace(
            FieldStatus.BELOW_THRESHOLD, unlock_map={"a": ["x"]}, confidence=0.5, field_name="a"
        ),
        "b": _trace(
            FieldStatus.BELOW_THRESHOLD,
            unlock_map={"b": ["x", "y", "z"]},
            confidence=0.5,
            field_name="b",
        ),
    }
    targets = extract_corrective_targets(_inference(blocked, traces), _state(), _cfg())
    assert targets[0].field_name == "b"


def test_extract_max_cap():
    blocked = {f"f{i}": FieldStatus.INPUTS_MISSING for i in range(20)}
    targets = extract_corrective_targets(_inference(blocked), _state(), _cfg())
    assert len(targets) <= 8


def test_extract_ignores_other_statuses():
    blocked = {"x": "converged", "y": FieldStatus.INPUTS_MISSING}
    targets = extract_corrective_targets(_inference(blocked), _state(), _cfg())
    assert len(targets) == 1
    assert targets[0].field_name == "y"


def test_target_prompt_hint_format():
    t = CorrectiveTarget(
        field_name="hdpe_grade",
        status=FieldStatus.INPUTS_MISSING,
        unlock_score=3.0,
        missing_inputs=("contamination_pct", "mfi_value"),
        confidence_gap=0.25,
    )
    hint = t.to_prompt_hint()
    assert "hdpe_grade" in hint
    assert "contamination_pct" in hint


# ── CorrectiveState ────────────────────────────────────────────────────────────


def test_corrective_state_deterministic():
    targets = [CorrectiveTarget("a", FieldStatus.INPUTS_MISSING, 2.0)]
    s1 = CorrectiveState.build(targets, "run-1", 2)
    s2 = CorrectiveState.build(targets, "run-1", 2)
    assert s1.idempotency_hash == s2.idempotency_hash


def test_corrective_state_different_run_different_hash():
    targets = [CorrectiveTarget("a", FieldStatus.INPUTS_MISSING, 2.0)]
    s1 = CorrectiveState.build(targets, "run-1", 2)
    s2 = CorrectiveState.build(targets, "run-2", 2)
    assert s1.idempotency_hash != s2.idempotency_hash


# ── apply / consume ────────────────────────────────────────────────────────────


def test_apply_injects_key():
    state = _state()
    corrective = CorrectiveState.build(
        [CorrectiveTarget("f", FieldStatus.INPUTS_MISSING, 1.0)], "r", 2
    )
    new_state = apply_corrective_override(state, corrective)
    assert _CORRECTIVE_TARGETS_KEY in new_state.metadata


def test_consume_pops_key():
    state = _state()
    corrective = CorrectiveState.build(
        [CorrectiveTarget("f", FieldStatus.INPUTS_MISSING, 1.0)], "r", 2
    )
    state_with = apply_corrective_override(state, corrective)
    state_clean, consumed = consume_corrective_override(state_with)
    assert consumed is not None
    assert _CORRECTIVE_TARGETS_KEY not in state_clean.metadata


def test_consume_idempotent_when_empty():
    state = _state()
    new_state, consumed = consume_corrective_override(state)
    assert consumed is None
    assert new_state.metadata == {}
