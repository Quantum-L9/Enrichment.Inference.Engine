"""
Inference Bridge Adapter — v1 API surface backed by v2 engine.

Wraps build_derivation_graph() + run_inference() from inference_bridge_v2
behind the InferenceBridge class interface that convergence_controller.py consumes.

Migration path:
  1. Controller imports this adapter instead of inference_bridge.py (v1)
  2. All v1 API calls (.run(), .get_rule_catalog(), result.derived_fields) work unchanged
  3. NEW: .unlock_map property exposes v2's strategic search targeting signal
  4. Once all consumers are migrated, delete inference_bridge.py (v1)

Consumer contract (unchanged):
  bridge = InferenceBridge(rules=..., domain_spec=...)
  catalog = bridge.get_rule_catalog()
  result = bridge.run(entity, confidence_map)
  result.derived_fields   → dict[str, Any]
  result.confidence_map   → dict[str, float]
  result.rules_fired      → int
  result.rules_skipped    → int
  result.rule_trace       → list[dict]

New v2 capabilities exposed:
  result.unlock_map       → dict[str, float]  (search targeting signal)
  result.blocked_fields   → dict[str, str]    (why each field didn't fire)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .inference_bridge_v2 import (
    DerivationGraph,
    InferenceStatus,
    build_derivation_graph,
    run_inference,
)
from .inference_bridge_v2 import (
    InferenceResult as V2InferenceResult,
)


@dataclass
class InferenceResult:
    """v1-compatible result shape with v2 extras."""

    derived_fields: dict[str, Any] = field(default_factory=dict)
    confidence_map: dict[str, float] = field(default_factory=dict)
    rules_fired: int = 0
    rules_skipped: int = 0
    rule_trace: list[dict] = field(default_factory=list)
    # v2 extras — available to consumers that opt in
    unlock_map: dict[str, float] = field(default_factory=dict)
    blocked_fields: dict[str, str] = field(default_factory=dict)


class InferenceBridge:
    """
    Adapter: v1 class interface backed by v2 derivation graph engine.

    Accepts either:
      - rules: list[dict]  (v1 flat rules — builds catalog only, no graph)
      - domain_spec: dict   (v2 YAML spec — builds full derivation graph)
    If domain_spec is provided, the v2 engine is used for .run().
    If only rules are provided, catalog works but .run() returns empty
    (v1 rules don't have derived_from declarations needed by v2 graph).
    """

    def __init__(
        self,
        rules: list[dict] | None = None,
        domain_spec: dict[str, Any] | None = None,
    ):
        self._rules = rules or []
        self._domain_spec = domain_spec
        self._graph: DerivationGraph | None = None
        self._last_unlock_map: dict[str, float] = {}

        if domain_spec:
            self._graph = build_derivation_graph(domain_spec)

    def run(
        self,
        entity: dict[str, Any],
        confidence_map: dict[str, float] | None = None,
    ) -> InferenceResult:
        """Execute inference and return v1-shaped result."""
        if _canonical_inference_rules(self._rules):
            return self._run_rule_engine(entity)
        if self._graph is None:
            # No domain_spec provided — cannot run v2 inference
            return InferenceResult()

        v2_result: V2InferenceResult = run_inference(
            graph=self._graph,
            entity=entity,
            confidence_map=confidence_map or {},
        )

        # Translate v2 → v1 result shape
        derived_fields: dict[str, Any] = {}
        conf_map: dict[str, float] = {}
        trace: list[dict] = []
        blocked: dict[str, str] = {}

        for name, fir in v2_result.derived.items():
            if fir.status == InferenceStatus.DERIVED:
                derived_fields[name] = fir.value
                conf_map[name] = fir.confidence
                trace.append(
                    {
                        "rule": fir.rule_used or "derived",
                        "status": "fired",
                        "produced": [name],
                        "confidence": fir.confidence,
                    }
                )
            elif fir.status == InferenceStatus.ALREADY_SET:
                # Already set fields pass through (v1 didn't track these)
                derived_fields[name] = fir.value
                conf_map[name] = fir.confidence

        for name, fir in v2_result.blocked.items():
            blocked[name] = fir.status.value
            trace.append(
                {
                    "rule": fir.rule_used or "blocked",
                    "status": fir.status.value,
                    "field": name,
                    "missing": fir.missing_inputs,
                }
            )

        rules_fired = v2_result.stats.get("derived", 0)
        rules_skipped = sum(v for k, v in v2_result.stats.items() if k != "derived")

        self._last_unlock_map = v2_result.unlock_map

        return InferenceResult(
            derived_fields=derived_fields,
            confidence_map=conf_map,
            rules_fired=rules_fired,
            rules_skipped=rules_skipped,
            rule_trace=trace,
            unlock_map=v2_result.unlock_map,
            blocked_fields=blocked,
        )

    def get_rule_catalog(self) -> list[dict]:
        """Return rule metadata for MetaPromptPlanner.

        If graph is available, builds catalog from derivation edges.
        Otherwise falls back to v1-style flat rules.
        """
        if self._graph:
            return [
                {
                    "name": edge.inference_rule or edge.target,
                    "requires": list(edge.inputs),
                    "produces": [edge.target],
                }
                for edge in self._graph.edges
            ]
        if _canonical_inference_rules(self._rules):
            catalog: list[dict] = []
            for rule in self._rules:
                conditions = rule.get("conditions") or []
                outputs = rule.get("outputs") or []
                catalog.append(
                    {
                        "name": rule.get("rule_id") or rule.get("name", "unnamed"),
                        "requires": [
                            str(item.get("field"))
                            for item in conditions
                            if isinstance(item, dict) and item.get("field")
                        ],
                        "produces": [
                            str(item.get("field"))
                            for item in outputs
                            if isinstance(item, dict) and item.get("field")
                        ],
                    }
                )
            return catalog
        # Fallback: v1-style flat rules
        return [
            {
                "name": r.get("name", "unnamed"),
                "requires": r.get("requires", []),
                "produces": list(r.get("produces", {}).keys()),
            }
            for r in self._rules
        ]

    @property
    def unlock_map(self) -> dict[str, float]:
        """Expose last-run unlock_map for search targeting.

        Returns empty dict if .run() hasn't been called yet.
        """
        return self._last_unlock_map

    def _run_rule_engine(self, entity: dict[str, Any]) -> InferenceResult:
        from .inference.rule_engine import infer
        from .inference.rule_loader import load_rules_data

        engine = infer(entity, load_rules_data(self._rules))
        return InferenceResult(
            derived_fields=engine.derived_fields,
            confidence_map=engine.confidence_map,
            rules_fired=len(engine.rules_fired),
            rules_skipped=engine.rules_skipped,
            rule_trace=[
                {
                    "rule": item.rule_id,
                    "status": "fired",
                    "produced": list(item.outputs_produced),
                    "confidence": item.confidence,
                }
                for item in engine.rules_fired
            ],
            unlock_map={},
            blocked_fields={},
        )

    @property
    def graph(self) -> DerivationGraph | None:
        """Direct access to v2 graph for advanced consumers."""
        return self._graph


def _canonical_inference_rules(rules: list[dict[str, Any]]) -> bool:
    if not rules:
        return False
    first = rules[0]
    if not isinstance(first, dict):
        return False
    conditions = first.get("conditions")
    return isinstance(conditions, list) and any(
        isinstance(item, dict) and item.get("field") and item.get("operator") for item in conditions
    )
