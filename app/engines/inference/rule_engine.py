"""Deterministic inference engine — fires rules against enriched feature vectors."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
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
#
# Entity values are provider output: any JSON shape can arrive under any field.
# Every operator is therefore total — a shape it cannot interpret evaluates to
# False, never to an exception. Collection members are compared by equality,
# not hashed, so a list of objects is a legitimate non-match rather than a
# TypeError; `in` is only applied to the shapes that support it. A mapping
# CONTAINS target is the one subset comparison (see `_mapping_matches`).

_COLLECTIONS = (list, tuple, set, frozenset)


def _mapping_matches(element: Any, target: Mapping[Any, Any]) -> bool:
    """True iff every key `target` names is in `element` with an equal normalised value.

    Keys `element` carries beyond the target neither match nor fail: a declared
    `list` of `map` names more properties than any one rule tests. An empty
    target names nothing and so matches nothing; it is not a wildcard.
    """
    if not target or not isinstance(element, Mapping):
        return False
    return all(
        key in element and _normalise(element[key]) == _normalise(want)
        for key, want in target.items()
    )


def _eval_contains(value: Any, target: Any) -> bool:
    """Check if target is contained within value (collection or string).

    A mapping target is matched against mapping members by the keys it names
    (`_mapping_matches`); any other target by normalised equality.
    """
    needle = _normalise(target)
    if isinstance(value, _COLLECTIONS):
        if isinstance(target, Mapping):
            return any(_mapping_matches(v, target) for v in value)
        return any(_normalise(v) == needle for v in value)
    if isinstance(value, str):
        return isinstance(needle, str) and needle in _normalise(value)
    return False


def _eval_in(value: Any, target: Any) -> bool:
    """Check if value is a member of the target collection."""
    member = _normalise(value)
    if isinstance(target, _COLLECTIONS):
        return any(member == _normalise(t) for t in target)
    return bool(member == _normalise(target))


def _eval_not_in(value: Any, target: Any) -> bool:
    """Check if value is absent from the target collection."""
    return not _eval_in(value, target)


def _finite_float(value: Any) -> float | None:
    """`value` as a finite float, else None.

    A boolean is not a number here (as in spec_types._check_float), and NaN or
    an infinity — which Python's JSON decoder accepts as `NaN` / `Infinity`, and
    `float()` parses from provider text — would otherwise satisfy GT/LT bounds.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _finite_floats(left: Any, right: Any) -> tuple[float, float] | None:
    left_num, right_num = _finite_float(left), _finite_float(right)
    if left_num is None or right_num is None:
        return None
    return left_num, right_num


def _eval_numeric(op: Operator, value: Any, target: Any) -> bool:
    pair = _finite_floats(value, target)
    if pair is None:
        return False
    left, right = pair
    if op is Operator.GT:
        return left > right
    if op is Operator.LT:
        return left < right
    if op is Operator.GTE:
        return left >= right
    return left <= right


def _eval_collection(op: Operator, value: Any, target: Any) -> bool:
    if op is Operator.CONTAINS:
        return _eval_contains(value, target)
    if op is Operator.IN:
        return _eval_in(value, target)
    if op is Operator.NOT_IN:
        return _eval_not_in(value, target)
    return False


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
        return bool(_normalise(value) == _normalise(target))
    if op in {Operator.GT, Operator.LT, Operator.GTE, Operator.LTE}:
        return _eval_numeric(op, value, target)
    if op in {Operator.CONTAINS, Operator.IN, Operator.NOT_IN}:
        return _eval_collection(op, value, target)
    return False


def condition_holds(condition: RuleCondition, value: Any) -> bool:
    """Public form of the single-condition test `infer` applies."""
    return _evaluate_condition(condition, value)


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


#: `admit(field, value)`: may this value become a derived field? (EIE-PR-U01)
Admit = Callable[[str, Any], bool]


def _fire_cascade_candidate(
    rule: RuleDefinition,
    working: dict[str, Any],
    cascade: int,
    best_output: dict[str, tuple[Any, int, float, str]],
    derivation_chains: dict[str, list[str]],
    new_fields: dict[str, Any],
    admit: Admit | None = None,
) -> RuleFired | None | bool:
    """Fire one rule candidate; update output tracking.

    Returns the RuleFired record, None when its conditions do not hold, or
    False when they hold but `admit` refuses every output. A refused output
    never claims its field and never enters `working`, so it can neither feed
    a later rule nor suppress a lower-priority output that is admitted.
    """
    result = _try_fire(rule, working)
    if result is None:
        return None
    if admit is not None:
        result["outputs"] = {f: v for f, v in result["outputs"].items() if admit(f, v)}
        if not result["outputs"]:
            return False

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
        if existing is not None and field_name in working:
            # Settled on an earlier cascade and already consumed by the rules
            # that fired since; replacing it now would report a value that no
            # downstream derivation actually used.
            continue
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
    fired_ids: set[str],
    admit: Admit | None = None,
) -> tuple[list[RuleFired], int, int, int]:
    """Run one cascade pass; return (fired_records, evaluated, skipped, new_field_count).

    A rule that fired on an earlier cascade is not evaluated again: `working`
    only ever gains fields, so its conditions would see the same values and
    produce the same outputs, inflating `rules_fired` with duplicates.
    """
    available = set(working.keys())
    candidates = registry.candidates_for(available)
    new_fields: dict[str, Any] = {}
    fired: list[RuleFired] = []
    evaluated = 0
    skipped = 0

    for rule in candidates:
        if rule.rule_id in fired_ids:
            continue
        if not rule.trigger_fields.issubset(available):
            skipped += 1
            continue
        evaluated += 1
        record = _fire_cascade_candidate(
            rule, working, cascade, best_output, derivation_chains, new_fields, admit
        )
        if isinstance(record, RuleFired):
            fired.append(record)
        if record is not None:  # fired, or refused: the same inputs refuse again
            fired_ids.add(rule.rule_id)

    working.update(new_fields)
    return fired, evaluated, skipped, len(new_fields)


def infer(
    entity_fields: dict[str, Any], registry: RuleRegistry, admit: Admit | None = None
) -> InferenceResult:
    working = dict(entity_fields)
    all_fired: list[RuleFired] = []
    derivation_chains: dict[str, list[str]] = {}
    best_output: dict[str, tuple[Any, int, float, str]] = {}
    total_evaluated = 0
    total_skipped = 0
    fired_ids: set[str] = set()

    for cascade in range(MAX_CASCADE_DEPTH + 1):
        fired, evaluated, skipped, new_field_count = _run_cascade_pass(
            cascade, working, registry, best_output, derivation_chains, fired_ids, admit
        )
        all_fired.extend(fired)
        total_evaluated += evaluated
        total_skipped += skipped

        # No new field means no rule can newly satisfy its conditions next time.
        if not new_field_count or cascade == MAX_CASCADE_DEPTH:
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
        cascade_depth=max(f.cascade_level for f in all_fired) + 1 if all_fired else 0,
    )
