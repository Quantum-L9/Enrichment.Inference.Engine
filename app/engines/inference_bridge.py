"""
Inference Bridge — Deterministic KB rules → derived fields.

Loads YAML rules (Option A: rules/ directory) and fires them against
enriched entity fields to produce computed fields like material_grade,
facility_tier, buyer_class, application_class.

Each rule has:
  - name: human-readable identifier
  - requires: list of field names that must be present
  - conditions: list of IF-THEN checks
  - produces: dict of field_name → computation
  - confidence: base confidence for derived fields

Aligned with graph repo's DomainSpec field metadata:
  managed_by: computed, derived_from: [...], discovery_confidence: 0.85
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.utils.safe_convert import safe_float


@dataclass
class InferenceResult:
    """Output of a single inference pass."""

    derived_fields: dict[str, Any] = field(default_factory=dict)
    confidence_map: dict[str, float] = field(default_factory=dict)
    rules_fired: int = 0
    rules_skipped: int = 0
    rule_trace: list[dict] = field(default_factory=list)


class InferenceBridge:
    """
    Fires deterministic rules against enriched entity data.

    Rules are loaded from YAML (see rules/plastics_recycling.yaml).
    Each rule declares required inputs and produced outputs.
    Rules fire in dependency order — a rule that produces field X
    runs before a rule that requires field X.
    """

    def __init__(self, rules: list[dict] | None = None):
        self._rules = rules or []
        self._sorted = self._topological_sort(self._rules)

    def run(
        self,
        entity: dict[str, Any],
        confidence_map: dict[str, float] | None = None,
    ) -> InferenceResult:
        result = InferenceResult()
        working = {**entity}
        conf_map = dict(confidence_map or {})

        for rule in self._sorted:
            name = rule.get("name", "unnamed")
            requires = rule.get("requires", [])
            min_confidence = rule.get("min_confidence", 0.5)

            # Check all required fields present
            missing = [r for r in requires if r not in working or working[r] is None]
            if missing:
                result.rules_skipped += 1
                continue

            # Check confidence threshold on inputs
            input_confidences = [conf_map.get(r, 0.7) for r in requires]
            if min(input_confidences, default=1.0) < min_confidence:
                result.rules_skipped += 1
                result.rule_trace.append(
                    {
                        "rule": name,
                        "status": "skipped",
                        "reason": f"input confidence below {min_confidence}",
                    }
                )
                continue

            # Fire conditions
            try:
                derived = self._evaluate_rule(rule, working)
            except Exception as e:
                result.rule_trace.append(
                    {
                        "rule": name,
                        "status": "error",
                        "reason": str(e),
                    }
                )
                continue

            if derived:
                result.rules_fired += 1
                base_conf = rule.get("confidence", 0.8)
                avg_input_conf = sum(input_confidences) / max(len(input_confidences), 1)
                derived_conf = round(base_conf * avg_input_conf, 4)

                for field_name, value in derived.items():
                    result.derived_fields[field_name] = value
                    result.confidence_map[field_name] = derived_conf
                    working[field_name] = value
                    conf_map[field_name] = derived_conf

                result.rule_trace.append(
                    {
                        "rule": name,
                        "status": "fired",
                        "produced": list(derived.keys()),
                        "confidence": derived_conf,
                    }
                )

        return result

    def get_rule_catalog(self) -> list[dict]:
        """Return rule metadata for the MetaPromptPlanner."""
        return [
            {
                "name": r.get("name", "unnamed"),
                "requires": r.get("requires", []),
                "produces": list(r.get("produces", {}).keys()),
            }
            for r in self._rules
        ]

    def _check_single_condition(self, condition: dict, entity: dict[str, Any]) -> bool:
        """Return False if this condition fails, True if it passes."""
        field_name = condition.get("field")
        operator = condition.get("operator", "exists")
        value = condition.get("value")
        entity_val = entity.get(field_name)

        if entity_val is None:
            return False
        if operator == "exists":
            return True
        if operator == "eq":
            return entity_val == value
        if operator == "in":
            return entity_val in (value or [])
        if operator == "gte":
            try:
                return float(entity_val) >= float(value)
            except (ValueError, TypeError):
                return False
        if operator == "lte":
            try:
                return float(entity_val) <= float(value)
            except (ValueError, TypeError):
                return False
        if operator == "contains":
            return value in str(entity_val)
        return True

    def _build_derived_outputs(self, produces: dict, entity: dict[str, Any]) -> dict[str, Any]:
        """Compute all output fields declared in 'produces'."""
        derived: dict[str, Any] = {}
        for field_name, computation in produces.items():
            if isinstance(computation, dict):
                derived[field_name] = self._compute_field(computation, entity)
            else:
                derived[field_name] = computation
        return derived

    def _evaluate_rule(self, rule: dict, entity: dict[str, Any]) -> dict[str, Any]:
        """Evaluate a single rule's conditions and compute outputs."""
        conditions = rule.get("conditions", [])
        produces = rule.get("produces", {})

        for condition in conditions:
            if not self._check_single_condition(condition, entity):
                return {}

        return self._build_derived_outputs(produces, entity)

    def _compute_field(self, computation: dict, entity: dict[str, Any]) -> Any:
        """Execute a field computation."""
        method = computation.get("method", "static")

        if method == "static":
            return computation.get("value")

        elif method == "lookup":
            table = computation.get("table", {})
            key_field = computation.get("key_field")
            key_val = str(entity.get(key_field, ""))
            return table.get(key_val, computation.get("default"))

        elif method == "threshold":
            source = computation.get("source_field")
            val = entity.get(source)
            try:
                val = float(val)
            except (ValueError, TypeError):
                return computation.get("default")
            thresholds = computation.get("thresholds", [])
            for t in thresholds:
                if val <= t.get("max", safe_float("inf")):
                    return t.get("label")
            return computation.get("default")

        elif method == "concatenate":
            parts = [str(entity.get(f, "")) for f in computation.get("fields", [])]
            sep = computation.get("separator", "-")
            return sep.join(p for p in parts if p)

        return computation.get("default")

    @staticmethod
    def _topological_sort(rules: list[dict]) -> list[dict]:
        """Sort rules so producers run before consumers."""
        produces_map: dict[str, str] = {}
        for r in rules:
            for f in r.get("produces", {}):
                produces_map[f] = r.get("name", "")

        sorted_rules: list[dict] = []
        remaining = list(rules)
        resolved: set[str] = set()
        max_iterations = len(rules) * 2

        for _ in range(max_iterations):
            if not remaining:
                break
            progress = False
            next_remaining = []
            for r in remaining:
                deps = set(r.get("requires", []))
                unresolved_deps = deps - resolved
                producer_deps = {d for d in unresolved_deps if d in produces_map}
                if not producer_deps:
                    sorted_rules.append(r)
                    resolved.update(r.get("produces", {}).keys())
                    progress = True
                else:
                    next_remaining.append(r)
            remaining = next_remaining
            if not progress:
                sorted_rules.extend(remaining)
                break

        return sorted_rules
