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

Two rule contracts, two existing executors — no third:
  - `derived_from` declarations on dict-shaped ontology properties
    → inference_bridge_v2 (domain_spec=...).
  - Domain `inference_rules` (conditions → outputs, e.g. domains/plasticos)
    → inference.rule_loader + inference.rule_engine.infer (rules=...).
The two are not translated into each other: v2 has no conditions or literal
outputs, and condition rules have no `derived_from` edges.

Condition-rule results are composed here, at the one place that holds both the
rule outcome and the convergence confidence map:
  - a field already present in the entity is never re-derived or overwritten
    (the same no-overwrite the rule engine applies to its own working set).
    The entity is therefore facts only — record, provider and graph-seeded
    values. The convergence loop keeps this bridge's own earlier derivations
    out of it and re-runs the rules every pass, so a derivation always
    reflects the inputs currently in the response (see convergence_controller);
  - confidence = min(rule confidence, confidence of every condition input).
    A value the request's entity supplied carries no confidence entry; it is
    the record's own data and bounds nothing (1.0);
  - every derived field carries provenance: rule id, rule-set version, the
    inputs and their confidences, and the cascade level;
  - search targeting: a rule that has not produced its output and whose other
    condition fields are present and hold credits its one missing condition
    field 1.0 in `unlock_map` (summed with the derivation graph's signal), the
    same sole-blocker credit `inference_bridge_v2` gives a derivation edge. It
    only reorders fields already targeted (#237);
  - the entity is provider output and may hold any JSON shape. The rule engine
    is total over such values; should execution still fail, the fault is
    isolated here and reported in describe()["errors"] — the converge that
    carried the values completes without the derivations, it does not fail.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import structlog

from .inference.rule_engine import RuleFired, condition_holds, infer
from .inference.rule_loader import RuleDefinition, RuleRegistry, bound_node, parse_rules
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
    # Condition-rule derivations: field → provenance record
    provenance: dict[str, dict[str, Any]] = field(default_factory=dict)


logger = structlog.get_logger("inference_bridge_adapter")

# describe()["status"] values
STATUS_ACTIVE = "active"  # at least one executor has something to run
STATUS_INVALID = "invalid_rules"  # rule set refused; nothing runs
STATUS_CATALOG_ONLY = "catalog_only"  # legacy flat rules: catalog, no execution
STATUS_NOT_CONFIGURED = "not_configured"  # no domain inference context

# Confidence of an input with no confidence-map entry: the record's own data.
_RECORD_CONFIDENCE = 1.0


def _is_condition_rule_set(rules: list[Any]) -> bool:
    """True when the list uses the domain `inference_rules` contract."""
    return any(
        isinstance(r, dict) and ("conditions" in r or "outputs" in r or "rule_id" in r)
        for r in rules
    )


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
        node: str | None = None,
    ):
        """`node` is the request's resolved ontology node (see `node_for_subject`).

        Given a `node` and a spec, inference is bound to that node: only the
        condition rules bound to it run (a rule that cannot be bound to one
        declared node refuses the set), and the derivation graph is built from
        that node's declarations. With `node=None` the bridge runs exactly what
        it ran before node binding existed; it never picks a node for the
        request. Runtime filtering therefore activates only when the caller
        passes `node` (caller wiring: convergence_controller.py / handlers.py).
        The spec contract (domain_spec_schema._rules) refuses unbound rules at
        load and publish whatever the caller passes.
        """
        self._rules: list[dict] = []
        self._domain_spec = domain_spec
        self._graph: DerivationGraph | None = None
        self._registry: RuleRegistry | None = None
        self._rule_errors: list[str] = []
        # Faults raised while executing an accepted rule set. They do not change
        # `status`: the rules were valid, a run over some entity was not.
        self._run_errors: list[str] = []
        self._last_unlock_map: dict[str, float] = {}

        # Binding needs both a node and the spec that declares the node labels.
        labels = _node_labels(domain_spec) if domain_spec and node is not None else None
        self._node = node

        if domain_spec:
            scoped = domain_spec
            if labels is not None and node is not None:  # undeclared node: no declarations
                scoped = _scope_to_node(domain_spec, node)
            self._graph = build_derivation_graph(scoped)

        if rules is None or rules == []:
            return
        if not isinstance(rules, list) or _is_condition_rule_set(rules):
            try:
                registry = parse_rules(rules, source="domain inference_rules", strict=True)
                if labels is not None:
                    registry = _rules_for_node(registry, labels, node)
                self._registry = registry
            except ValueError as exc:
                self._rule_errors = [str(exc)]
                logger.error("inference_rules_refused", error=str(exc))
        else:
            self._rules = rules

    @property
    def status(self) -> str:
        if self._rule_errors:
            return STATUS_INVALID
        if self._registry is not None or (self._graph is not None and self._graph.edges):
            return STATUS_ACTIVE
        if self._rules:
            return STATUS_CATALOG_ONLY
        return STATUS_NOT_CONFIGURED

    def describe(self) -> dict[str, Any]:
        """What this bridge will execute, for the response's feature_vector."""
        engines: list[str] = []
        if self._graph is not None and self._graph.edges:
            engines.append("derivation_graph")
        if self._registry is not None:
            engines.append("condition_rules")
        versions = sorted(
            {r.rule_or_model_version for r in self._registry.all_rules() if r.rule_or_model_version}
            if self._registry is not None
            else set()
        )
        return {
            "status": self.status,
            "engines": engines,
            "rule_count": self._registry.count() if self._registry is not None else 0,
            "derivation_edges": len(self._graph.edges) if self._graph is not None else 0,
            "rule_versions": versions,
            "errors": [*self._rule_errors, *self._run_errors],
        }

    def run(
        self,
        entity: dict[str, Any],
        confidence_map: dict[str, float] | None = None,
    ) -> InferenceResult:
        """Execute inference and return v1-shaped result.

        A refused rule set (STATUS_INVALID) runs nothing, the derivation graph
        included: a refusal is of the domain's inference, not of one executor.
        """
        if self._rule_errors:
            self._last_unlock_map = {}
            return InferenceResult()
        result = self._run_derivation_graph(entity, confidence_map or {})
        if self._registry is not None:
            self._run_condition_rules(self._registry, entity, confidence_map or {}, result)
            known = {k: v for k, v in entity.items() if v is not None}
            known.update(result.derived_fields)
            unlock = dict(result.unlock_map)
            for name, credit in _condition_unlock_values(self._registry, known).items():
                unlock[name] = unlock.get(name, 0.0) + credit
            result.unlock_map = unlock
            self._last_unlock_map = unlock
        return result

    def _run_derivation_graph(
        self,
        entity: dict[str, Any],
        confidence_map: dict[str, float],
    ) -> InferenceResult:
        if self._graph is None:
            # No domain_spec provided — cannot run v2 inference
            return InferenceResult()

        v2_result: V2InferenceResult = run_inference(
            graph=self._graph,
            entity=entity,
            confidence_map=confidence_map,
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
            # ALREADY_SET is a fact the entity carried, not a derivation: it is
            # never reported as derived (the caller would record it as inferred).

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

    def _run_condition_rules(
        self,
        registry: RuleRegistry,
        entity: dict[str, Any],
        confidence_map: dict[str, float],
        result: InferenceResult,
    ) -> None:
        """Execute domain `inference_rules` via rule_engine.infer; fold into result."""
        # Facts already known, including anything the derivation graph produced.
        present = {k: v for k, v in entity.items() if v is not None}
        present.update(result.derived_fields)
        known_conf = {**confidence_map, **result.confidence_map}

        try:
            outcome = infer(present, registry)
        except Exception as exc:
            # Provider values are untrusted shapes; a fault evaluating them is a
            # degraded inference, not a failed converge. Report it where the
            # response already explains why no rule fired.
            message = f"condition rules did not execute: {type(exc).__name__}: {exc}"
            if message not in self._run_errors:
                self._run_errors.append(message)
            logger.exception("inference_rules_execution_failed", error=str(exc))
            result.rules_skipped += registry.count()
            return

        first_firing: dict[str, RuleFired] = {}
        for fired in outcome.rules_fired:
            first_firing.setdefault(fired.rule_id, fired)

        winners = {
            name: first_firing[outcome.derivation_chains[name][-1]]
            for name in outcome.derived_fields
            if name not in present and outcome.derivation_chains.get(name)
        }
        produced_by: set[str] = set()
        # Cascade order, so a chained input's bounded confidence exists first.
        for name in sorted(winners, key=lambda f: (winners[f].cascade_level, f)):
            fired = winners[name]
            rule = registry.get(fired.rule_id)
            if rule is None:  # pragma: no cover - registry and outcome share rules
                continue
            record = _provenance_record(
                rule, fired, name, {**present, **result.derived_fields}, known_conf
            )
            produced_by.add(fired.rule_id)
            result.derived_fields[name] = outcome.derived_fields[name]
            result.confidence_map[name] = known_conf[name] = record["confidence"]
            result.provenance[name] = record
            result.rule_trace.append(
                {
                    "rule": fired.rule_id,
                    "status": "fired",
                    "produced": [name],
                    "confidence": record["confidence"],
                }
            )

        result.rules_fired += len(produced_by)
        result.rules_skipped += registry.count() - len(produced_by)

    def get_rule_catalog(self) -> list[dict]:
        """Return rule metadata for MetaPromptPlanner.

        Condition rules first, then derivation edges, else v1-style flat rules.
        """
        if self._registry is not None:
            return [
                {
                    "name": r.rule_id,
                    "requires": sorted(r.trigger_fields),
                    "produces": [o.field for o in r.outputs],
                }
                for r in self._registry.all_rules()
            ]
        if self._graph:
            return [
                {
                    "name": edge.inference_rule or edge.target,
                    "requires": list(edge.inputs),
                    "produces": [edge.target],
                }
                for edge in self._graph.edges
            ]
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

    @property
    def rule_registry(self) -> RuleRegistry | None:
        """The strict registry of accepted domain `inference_rules`, if any."""
        return self._registry

    @property
    def graph(self) -> DerivationGraph | None:
        """Direct access to v2 graph for advanced consumers."""
        return self._graph


def _spec_nodes(domain_spec: dict[str, Any]) -> Any:
    """The spec's node declarations, read as build_derivation_graph reads them."""
    ontology = domain_spec.get("ontology", domain_spec)
    if not isinstance(ontology, dict):
        return []
    return ontology.get("nodes", ontology.get("entities", {}))


def _node_labels(domain_spec: dict[str, Any]) -> list[str]:
    """Declared node labels: native list entries' `label`, or legacy mapping keys."""
    nodes = _spec_nodes(domain_spec)
    if isinstance(nodes, dict):
        return [str(k) for k in nodes]
    if isinstance(nodes, list):
        return [
            n["label"] for n in nodes if isinstance(n, dict) and isinstance(n.get("label"), str)
        ]
    return []


def _scope_to_node(domain_spec: dict[str, Any], node: str) -> dict[str, Any]:
    """A shallow copy of the spec whose ontology declares only `node`.

    A malformed ontology (not a mapping, or nodes neither a list nor a mapping)
    scopes to no declarations rather than raising.
    """
    nodes = _spec_nodes(domain_spec)
    if isinstance(nodes, dict):
        kept: Any = {k: v for k, v in nodes.items() if str(k) == node}
    elif isinstance(nodes, list):
        kept = [n for n in nodes if isinstance(n, dict) and n.get("label") == node]
    else:
        kept = []
    ontology = domain_spec.get("ontology", domain_spec)
    if not isinstance(ontology, dict):
        return {**domain_spec, "ontology": {"nodes": []}}
    key = "nodes" if "nodes" in ontology else "entities"
    scoped_ontology = {**ontology, key: kept}
    if "ontology" in domain_spec:
        return {**domain_spec, "ontology": scoped_ontology}
    return scoped_ontology


def _rules_for_node(registry: RuleRegistry, labels: list[str], node: str | None) -> RuleRegistry:
    """The accepted rules bound to `node`; refuse the set if any rule is unbound.

    Raises:
        ValueError: a rule names no node in a multi-node spec, or an undeclared one
    """
    errors: list[str] = []
    scoped = RuleRegistry()
    for rule in registry.all_rules():
        try:
            bound = bound_node(rule.rule_id, rule.node, labels)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if bound == node:
            scoped.add_rule(rule)
    if errors:
        raise ValueError("Invalid inference_rules node binding: " + "; ".join(errors))
    return scoped


def node_for_subject(
    domain_spec: dict[str, Any] | None, subject: Mapping[str, Any] | None
) -> str | None:
    """The node a resolved request subject belongs to, from the spec's own vocabulary.

    `subjects.types[].node` for the type the subject names, when that node is
    declared; otherwise None (no node is guessed).
    """
    if not domain_spec or not subject:
        return None
    subjects = domain_spec.get("subjects")
    types = subjects.get("types") if isinstance(subjects, dict) else None
    if not isinstance(types, list):
        return None
    for entry in types:
        if isinstance(entry, dict) and entry.get("name") == subject.get("type"):
            node = entry.get("node")
            return node if node in _node_labels(domain_spec) else None
    return None


def _provenance_record(
    rule: RuleDefinition,
    fired: RuleFired,
    target: str,
    values: dict[str, Any],
    known_conf: dict[str, float],
) -> dict[str, Any]:
    """Provenance for one derived field; confidence bounded by its weakest input."""
    inputs = {c: values.get(c) for c in fired.conditions_met}
    input_confidence = {c: known_conf.get(c, _RECORD_CONFIDENCE) for c in inputs}
    output = next(o for o in rule.outputs if o.field == target)
    return {
        "rule_id": rule.rule_id,
        "rule_version": rule.rule_or_model_version,
        "derivation_type": output.derivation_type,
        "inputs": inputs,
        "input_confidence": input_confidence,
        "rule_confidence": rule.confidence,
        "confidence": min([rule.confidence, *input_confidence.values()]),
        "cascade_level": fired.cascade_level,
    }


def _condition_unlock_values(registry: RuleRegistry, known: dict[str, Any]) -> dict[str, float]:
    """Credit each field that is the sole missing input of a rule that could still fire.

    A rule counts only when one of its outputs is still unknown, exactly one of
    its distinct condition fields is absent, and every present condition field
    already satisfies its condition — finding that one field could fire it.
    """
    unlock: dict[str, float] = {}
    for rule in registry.all_rules():
        if all(known.get(output.field) is not None for output in rule.outputs):
            continue
        missing: set[str] = set()
        holds = True
        for condition in rule.conditions:
            value = known.get(condition.field)
            if value is None:
                missing.add(condition.field)
            elif not condition_holds(condition, value):
                holds = False
                break
        if holds and len(missing) == 1:
            (blocker,) = missing
            unlock[blocker] = unlock.get(blocker, 0.0) + 1.0
    return unlock
