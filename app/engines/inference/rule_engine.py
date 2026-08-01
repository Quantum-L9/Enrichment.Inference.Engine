"""Deterministic inference engine — fires rules against enriched feature vectors."""

from __future__ import annotations

from app.utils.safe_convert import safe_float
from typing import Any

import structlog
from pydantic import BaseModel, Field

from .rule_loader import Operator, RuleCondition, RuleDefinition, RuleRegistry

logger = structlog.get_logger(__name__)

MAX_CASCADE_DEPTH = 3


class RuleFired(BaseModel):
    rule_id: str
    conditions_met: list[str]
    outputs_produced: dict[str, Any]
    confidence: float
    priority: int
    cascade_level: int = 0


class InferenceResult(BaseModel):
    derived_fields: dict[str, Any] = Field(default_factory=dict)
    rules_fired: list[RuleFired] = Field(default_factory=list)
    rules_evaluated: int = 0
    rules_skipped: int = 0
    derivation_chains: dict[str, list[str]] = Field(default_factory=dict)
    inference_confidence: float = 1.0
    cascade_depth: int = 0


# ── condition helpers ─────────────────────────────────────────────────────────


def _eval_contains(value: Any, target: Any) -> bool:
    """Check if target is contained within value (list or string)."""
    if isinstance(value, (list, tuple, set, frozenset)):
        return _normalise(target) in {_normalise(v) for v in value}
    return _normalise(target) in _normalise(value)


def _eval_in(value: Any, target: Any) -> bool:
    """Check if value is a member of the target collection."""
    if isinstance(target, (list, tuple, set, frozenset)):
        return _normalise(value) in {_normalise(t) for t in target}
    return _normalise(value) == _normalise(target)


def _eval_not_in(value: Any, target: Any) -> bool:
    """Check if value is absent from the target collection."""
    if isinstance(target, (list, tuple, set, frozenset)):
        return _normalise(value) not in {_normalise(t) for t in target}
    return _normalise(value) != _normalise(target)


def _evaluate_condition(condition: RuleCondition, value: Any) -> bool:
    op = condition.operator
    target = condition.value

    if op is Operator.EXISTS:
        return value is not None
    if op is Operator.IS_TRUE:
        return bool(value)
    if op is Operator.IS_FALSE:
        return not bool(value)
    if value is None:
        return False
    if op is Operator.EQUALS:
        return _normalise(value) == _normalise(target)
    if op is Operator.GT:
        return safe_float(value) > safe_float(target)
    if op is Operator.LT:
        return safe_float(value) < safe_float(target)
    if op is Operator.GTE:
        return safe_float(value) >= safe_float(target)
    if op is Operator.LTE:
        return safe_float(value) <= safe_float(target)
    if op is Operator.CONTAINS:
        return _eval_contains(value, target)
    if op is Operator.IN:
        return _eval_in(value, target)
    if op is Operator.NOT_IN:
        return _eval_not_in(value, target)
    return False


def _normalise(v: Any) -> Any:
    if isinstance(v, str):
        return v.strip().lower()
    return v


def _try_fire(rule: RuleDefinition, entity: dict[str, Any]) -> dict[str, Any] | None:
    conditions_met: list[str] = []
    for cond in rule.conditions:
        field_val = entity.get(cond.field)
        if cond.operator is not Operator.EXISTS and field_val is None:
            return None
        if not _evaluate_condition(cond, field_val):
            return None
        conditions_met.append(cond.field)
    outputs: dict[str, Any] = {}
    for out in rule.outputs:
        outputs[out.field] = out.value_expr
    return {"conditions_met": conditions_met, "outputs": outputs}


# ── cascade helpers ───────────────────────────────────────────────────────────


def _fire_cascade_candidate(
    rule: RuleDefinition,
    working: dict[str, Any],
    cascade: int,
    best_output: dict[str, tuple[Any, int, float, str]],
    derivation_chains: dict[str, list[str]],
    new_fields: dict[str, Any],
) -> RuleFired | None:
    """Fire one rule candidate; update output tracking. Returns RuleFired or None."""
    result = _try_fire(rule, working)
    if result is None:
        return None

    fired_record = RuleFired(
        rule_id=rule.rule_id,
        conditions_met=result["conditions_met"],
        outputs_produced=result["outputs"],
        confidence=rule.confidence,
        priority=rule.priority,
        cascade_level=cascade,
    )

    for field_name, field_value in result["outputs"].items():
        existing = best_output.get(field_name)
        if existing is not None:
            _, ex_pri, ex_conf, _ = existing
            if (rule.priority, rule.confidence) <= (ex_pri, ex_conf):
                continue
        best_output[field_name] = (field_value, rule.priority, rule.confidence, rule.rule_id)
        derivation_chains.setdefault(field_name, []).append(rule.rule_id)
        if field_name not in working:
            new_fields[field_name] = field_value

    return fired_record


def _run_cascade_pass(
    cascade: int,
    working: dict[str, Any],
    registry: RuleRegistry,
    best_output: dict[str, tuple[Any, int, float, str]],
    derivation_chains: dict[str, list[str]],
) -> tuple[list[RuleFired], int, int]:
    """Run one cascade pass; return (fired_records, evaluated, skipped)."""
    available = set(working.keys())
    candidates = registry.candidates_for(available)
    new_fields: dict[str, Any] = {}
    fired: list[RuleFired] = []
    evaluated = 0
    skipped = 0

    for rule in candidates:
        if not rule.trigger_fields.issubset(available):
            skipped += 1
            continue
        evaluated += 1
        record = _fire_cascade_candidate(
            rule, working, cascade, best_output, derivation_chains, new_fields
        )
        if record is not None:
            fired.append(record)

    working.update(new_fields)
    return fired, evaluated, skipped


def infer(entity_fields: dict[str, Any], registry: RuleRegistry) -> InferenceResult:
    working = dict(entity_fields)
    all_fired: list[RuleFired] = []
    derivation_chains: dict[str, list[str]] = {}
    best_output: dict[str, tuple[Any, int, float, str]] = {}
    total_evaluated = 0
    total_skipped = 0

    for cascade in range(MAX_CASCADE_DEPTH + 1):
        fired, evaluated, skipped = _run_cascade_pass(
            cascade, working, registry, best_output, derivation_chains
        )
        all_fired.extend(fired)
        total_evaluated += evaluated
        total_skipped += skipped

        if not fired or cascade == MAX_CASCADE_DEPTH:
            break

    derived: dict[str, Any] = {k: v[0] for k, v in best_output.items()}
    confidences = [v[2] for v in best_output.values()]
    min_conf = min(confidences) if confidences else 1.0

    return InferenceResult(
        derived_fields=derived,
        rules_fired=all_fired,
        rules_evaluated=total_evaluated,
        rules_skipped=total_skipped,
        derivation_chains=derivation_chains,
        inference_confidence=round(min_conf, 4),
        cascade_depth=min(cascade + 1, MAX_CASCADE_DEPTH) if all_fired else 0,
    )
