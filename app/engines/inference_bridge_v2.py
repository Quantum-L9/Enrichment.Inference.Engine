"""
Inference Bridge v2 (inference_bridge.py)

Fully domain-agnostic. All derivation logic comes from the YAML.
No inference fires unless EVERY declared input field is present
AND meets confidence threshold. No speculative cross-entity guessing.

The derivation graph is built once from `derived_from` declarations
in the domain YAML ontology. This module:

  1. Builds a DAG from YAML `derived_from` edges
  2. Topologically sorts it (handles multi-hop chains)
  3. Runs only derivations where ALL inputs are satisfied
  4. Produces confidence scores bounded by input minimums
  5. Feeds unlock analysis back to search_optimizer_v2 for target selection

YAML contract consumed (same spec as field_classifier):
  ontology.nodes.{Entity}.properties.{field}:
    managed_by: computed | inference | derived
    derived_from: [field_a, field_b]          # REQUIRED for inference
    inference_rule: <rule_name>               # optional: named rule
    confidence_floor: 0.7                     # optional: minimum input conf

Integration:
  domain_spec.yaml → build_derivation_graph() → DerivationGraph
  DerivationGraph + entity_state → run_inference() → InferenceResult
  InferenceResult → search_optimizer_v2 (dynamic reclassification)
  InferenceResult → convergence_controller (coverage check)
"""

from __future__ import annotations

from app.utils.safe_convert import safe_float
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Derivation graph — built from YAML, immutable
# ──────────────────────────────────────────────


@dataclass(frozen=True)
class DerivationEdge:
    """A single derivation: target field derived from input fields."""

    target: str
    inputs: tuple[str, ...]  # ALL must be present + above threshold
    inference_rule: str | None  # named rule from YAML (or None = default)
    confidence_floor: float  # minimum confidence on ALL inputs (default 0.6)
    managed_by: str  # "computed" | "inference" | "derived"

    @property
    def input_set(self) -> set[str]:
        return set(self.inputs)


@dataclass
class DerivationGraph:
    """DAG of all derivation edges, topologically sorted."""

    domain: str
    edges: list[DerivationEdge]
    topo_order: list[str]  # target fields in safe execution order
    children_of: dict[str, list[str]]  # input_field → [target fields it enables]
    _edge_map: dict[str, DerivationEdge] = field(default_factory=dict, repr=False)


def _collect_edges_from_node(node_def: dict) -> list[DerivationEdge]:
    """Extract all DerivationEdge instances from a single node definition."""
    edges: list[DerivationEdge] = []
    props = node_def.get("properties", {})
    if not isinstance(props, dict):
        return edges
    for prop_name, prop_def in props.items():
        if not isinstance(prop_def, dict):
            continue
        derived_from = prop_def.get("derived_from")
        if not derived_from or not isinstance(derived_from, list):
            continue
        edges.append(
            DerivationEdge(
                target=prop_name,
                inputs=tuple(derived_from),
                inference_rule=prop_def.get("inference_rule"),
                confidence_floor=safe_float(prop_def.get("confidence_floor", 0.6)),
                managed_by=prop_def.get("managed_by", "computed"),
            )
        )
    return edges


def build_derivation_graph(domain_spec: dict[str, Any]) -> DerivationGraph:
    """Parse YAML ontology → DerivationGraph.

    Only fields with explicit `derived_from` lists become derivation edges.
    No implicit inference. No heuristic guessing.
    """
    domain_name = domain_spec.get("domain") or domain_spec.get("metadata", {}).get(
        "domain", "unknown"
    )

    ontology = domain_spec.get("ontology", domain_spec)
    nodes = ontology.get("nodes", ontology.get("entities", {}))

    edges: list[DerivationEdge] = []
    edge_map: dict[str, DerivationEdge] = {}

    node_iter = nodes.items() if isinstance(nodes, dict) else enumerate(nodes)
    for _, node_def in node_iter:
        if not isinstance(node_def, dict):
            continue
        for edge in _collect_edges_from_node(node_def):
            edges.append(edge)
            edge_map[edge.target] = edge

    # Build children_of: which targets does each input unlock?
    children_of: dict[str, list[str]] = {}
    for edge in edges:
        for inp in edge.inputs:
            children_of.setdefault(inp, []).append(edge.target)

    # Topological sort (Kahn's algorithm)
    topo_order = _topo_sort(edges, edge_map)

    logger.info(
        "derivation_graph.built",
        domain=domain_name,
        edges=len(edges),
        topo_depth=len(topo_order),
        multi_hop=[e.target for e in edges if any(i in edge_map for i in e.inputs)],
    )

    return DerivationGraph(
        domain=domain_name,
        edges=edges,
        topo_order=topo_order,
        children_of=children_of,
        _edge_map=edge_map,
    )


def _topo_sort(
    edges: list[DerivationEdge],
    edge_map: dict[str, DerivationEdge],
) -> list[str]:
    """Topological sort of derivation targets.

    Handles multi-hop: if field C derives from B, and B derives from A,
    then order is [B, C] so B is computed before C needs it.
    """
    in_degree: dict[str, int] = {}
    graph: dict[str, list[str]] = {}

    targets = {e.target for e in edges}
    for t in targets:
        in_degree.setdefault(t, 0)
        graph.setdefault(t, [])

    for edge in edges:
        for inp in edge.inputs:
            if inp in targets:
                # inp is itself a derived field → edge from inp to target
                graph.setdefault(inp, []).append(edge.target)
                in_degree[edge.target] = in_degree.get(edge.target, 0) + 1

    # Kahn's
    queue = deque(t for t in targets if in_degree.get(t, 0) == 0)
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for child in graph.get(node, []):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(order) != len(targets):
        cycle_fields = targets - set(order)
        logger.error(
            "derivation_graph.cycle_detected",
            fields=sorted(cycle_fields),
        )
        # Append remaining in arbitrary order (they won't fire due to
        # missing inputs, but we don't crash)
        order.extend(sorted(cycle_fields))

    return order


# ──────────────────────────────────────────────
# Inference rules registry
# ──────────────────────────────────────────────

# Rule functions: (input_values: dict, edge: DerivationEdge) → (value, confidence)
InferenceRuleFn = Callable[[dict[str, Any], DerivationEdge], tuple[Any, float]]

# Default rule: pass-through (value = dict of inputs, confidence = min of inputs)
# Domain-specific rules are registered at startup from YAML or Python plugins.
_RULE_REGISTRY: dict[str, InferenceRuleFn] = {}


def register_rule(name: str, fn: InferenceRuleFn) -> None:
    """Register a named inference rule function."""
    _RULE_REGISTRY[name] = fn
    logger.info("inference_rule.registered", rule=name)


def _default_rule(
    input_values: dict[str, Any],
    edge: DerivationEdge,
) -> tuple[Any, float]:
    """Default: return input values as composite, confidence = min of inputs.

    This is a placeholder. Real deployments should register domain rules
    that actually compute the derived value (e.g., lookup tables, formulas).
    """
    return input_values, 0.0  # 0.0 = "I have the inputs but no rule to compute"


# ──────────────────────────────────────────────
# Inference execution
# ──────────────────────────────────────────────


class InferenceStatus(StrEnum):
    DERIVED = "derived"  # all inputs present, rule fired, value produced
    INPUTS_MISSING = "inputs_missing"  # one or more inputs not available
    BELOW_THRESHOLD = "below_threshold"  # inputs present but confidence too low
    NO_RULE = "no_rule"  # inputs satisfied but no rule registered
    ALREADY_SET = "already_set"  # field already has a value above threshold
    CYCLE = "cycle"  # field is in a dependency cycle


@dataclass
class FieldInferenceResult:
    field: str
    status: InferenceStatus
    value: Any = None
    confidence: float = 0.0
    inputs_used: dict[str, Any] = field(default_factory=dict)
    inputs_confidence: dict[str, float] = field(default_factory=dict)
    missing_inputs: list[str] = field(default_factory=list)
    rule_used: str | None = None


@dataclass
class InferenceResult:
    """Complete inference pass result."""

    derived: dict[str, FieldInferenceResult]  # successfully derived
    blocked: dict[str, FieldInferenceResult]  # could not derive (and why)
    unlock_map: dict[str, float]  # field → unlock_value for optimizer
    stats: dict[str, int]

    @property
    def derived_count(self) -> int:
        return len(self.derived)

    @property
    def blocked_count(self) -> int:
        return len(self.blocked)

    def get_live_field_map_patches(self) -> dict[str, str]:
        """Return field_map patches for search_optimizer_v2.

        Fields successfully derived at high confidence → INFERRABLE
        Fields with all-but-one input → difficulty downgrade suggestion
        """
        from field_classifier import FieldDifficulty  # deferred import

        patches: dict[str, str] = {}
        for name, r in self.derived.items():
            if r.confidence >= 0.7:
                patches[name] = FieldDifficulty.INFERRABLE.value
        return patches


# ── inference helpers ─────────────────────────────────────────────────────────


def _check_edge_inputs(
    edge: DerivationEdge,
    working_entity: dict[str, Any],
    working_confidence: dict[str, float],
) -> tuple[list[str], list[str], dict[str, Any], dict[str, float]]:
    """Check all inputs for a derivation edge.

    Returns (missing, below_floor, input_values, input_confs).
    """
    missing: list[str] = []
    below_floor: list[str] = []
    input_values: dict[str, Any] = {}
    input_confs: dict[str, float] = {}

    for inp in edge.inputs:
        val = working_entity.get(inp)
        conf = working_confidence.get(inp, 0.0)
        if val is None:
            missing.append(inp)
        elif conf < edge.confidence_floor:
            below_floor.append(inp)
        else:
            input_values[inp] = val
            input_confs[inp] = conf

    return missing, below_floor, input_values, input_confs


def _execute_inference_rule(
    rule_name: str | None,
    rule_fn: InferenceRuleFn | None,
    input_values: dict[str, Any],
    input_confs: dict[str, float],
    edge: DerivationEdge,
    target_field: str,
) -> FieldInferenceResult:
    """Execute the rule function and return a FieldInferenceResult."""
    fn = rule_fn or _default_rule
    try:
        value, rule_confidence = fn(input_values, edge)
    except Exception as e:
        logger.error(
            "inference_rule.error",
            target=target_field,
            rule=rule_name,
            error=str(e),
        )
        return FieldInferenceResult(
            field=target_field,
            status=InferenceStatus.NO_RULE,
            inputs_used=input_values,
            rule_used=rule_name,
        )

    input_min_conf = min(input_confs.values()) if input_confs else 0.0
    if rule_fn is not None:
        # Real rule: confidence is bounded by weakest input
        final_confidence = min(input_min_conf, rule_confidence)
    else:
        # Default rule: inputs ready but no computation — don't produce a fake value
        final_confidence = 0.0
        value = None

    return FieldInferenceResult(
        field=target_field,
        status=InferenceStatus.DERIVED if value is not None else InferenceStatus.NO_RULE,
        value=value,
        confidence=final_confidence,
        inputs_used=input_values,
        inputs_confidence=input_confs,
        rule_used=rule_name or "_default",
    )


def _resolve_rule_fn(
    rule_name: str | None,
    target_field: str,
    input_values: dict[str, Any],
    input_confs: dict[str, float],
) -> tuple[InferenceRuleFn | None, FieldInferenceResult | None]:
    """Look up rule function; return (fn, None) on success or (None, blocked_result) if missing."""
    rule_fn = _RULE_REGISTRY.get(rule_name) if rule_name else None
    if rule_fn is None and rule_name is not None:
        return None, FieldInferenceResult(
            field=target_field,
            status=InferenceStatus.NO_RULE,
            inputs_used=input_values,
            inputs_confidence=input_confs,
            rule_used=rule_name,
        )
    return rule_fn, None


def run_inference(
    graph: DerivationGraph,
    entity: dict[str, Any],
    confidence_map: dict[str, float],
    existing_threshold: float = 0.7,
) -> InferenceResult:
    """Execute inference pass over an entity using the derivation graph.

    Rules:
    1. Process fields in topological order (multi-hop safe)
    2. A derivation fires ONLY if ALL inputs in derived_from are:
       - Present in entity (value is not None)
       - At or above the edge's confidence_floor
    3. If the field already has a value at >= existing_threshold, skip
    4. Newly derived values are added to a working copy so downstream
       derivations in the same pass can chain off them
    5. No speculative inference. No cross-entity inference. No guessing.

    Args:
        graph: DerivationGraph built from YAML
        entity: current entity field values
        confidence_map: {field_name: confidence} for each known field
        existing_threshold: skip fields already at this confidence
    """
    # Working copies (inference results feed into later derivations)
    working_entity = dict(entity)
    working_confidence = dict(confidence_map)

    derived: dict[str, FieldInferenceResult] = {}
    blocked: dict[str, FieldInferenceResult] = {}

    for target_field in graph.topo_order:
        edge = graph._edge_map.get(target_field)
        if edge is None:
            continue

        # Skip if already set at sufficient confidence
        existing_val = working_entity.get(target_field)
        existing_conf = working_confidence.get(target_field, 0.0)
        if existing_val is not None and existing_conf >= existing_threshold:
            derived[target_field] = FieldInferenceResult(
                field=target_field,
                status=InferenceStatus.ALREADY_SET,
                value=existing_val,
                confidence=existing_conf,
            )
            continue

        # Check ALL inputs present and above floor
        missing, below_floor, input_values, input_confs = _check_edge_inputs(
            edge, working_entity, working_confidence
        )

        if missing:
            blocked[target_field] = FieldInferenceResult(
                field=target_field,
                status=InferenceStatus.INPUTS_MISSING,
                missing_inputs=missing,
                inputs_confidence=input_confs,
            )
            continue

        if below_floor:
            blocked[target_field] = FieldInferenceResult(
                field=target_field,
                status=InferenceStatus.BELOW_THRESHOLD,
                missing_inputs=below_floor,
                inputs_confidence=input_confs,
            )
            continue

        # Resolve rule function (named rule must be registered)
        rule_fn, no_rule_result = _resolve_rule_fn(
            edge.inference_rule, target_field, input_values, input_confs
        )
        if no_rule_result is not None:
            blocked[target_field] = no_rule_result
            continue

        # Execute rule (or default)
        result = _execute_inference_rule(
            edge.inference_rule, rule_fn, input_values, input_confs, edge, target_field
        )

        if result.value is not None and result.confidence > 0.0:
            # Propagate into working copies for multi-hop chaining
            working_entity[target_field] = result.value
            working_confidence[target_field] = result.confidence
            derived[target_field] = result
        else:
            blocked[target_field] = result

    # Build unlock map for search optimizer
    unlock_map = _compute_unlock_values(graph, working_entity, working_confidence)

    # Stats
    status_counts: dict[str, int] = {}
    for r in list(derived.values()) + list(blocked.values()):
        status_counts[r.status.value] = status_counts.get(r.status.value, 0) + 1

    logger.info(
        "inference_bridge.complete",
        domain=graph.domain,
        derived=len(derived),
        blocked=len(blocked),
        stats=status_counts,
    )

    return InferenceResult(
        derived=derived,
        blocked=blocked,
        unlock_map=unlock_map,
        stats=status_counts,
    )


# ──────────────────────────────────────────────
# Unlock analysis — which field to search next?
# ──────────────────────────────────────────────


def _compute_unlock_values(
    graph: DerivationGraph,
    entity: dict[str, Any],
    confidence_map: dict[str, float],
) -> dict[str, float]:
    """For each MISSING field, compute how many downstream derivations
    it would unlock if found.

    unlock_value = count of derivation edges where this field is the
    ONLY missing input. Higher = search this field first.

    This is the strategic targeting signal for search_optimizer_v2.
    """
    unlock: dict[str, float] = {}

    for edge in graph.edges:
        missing_for_edge = []
        for inp in edge.inputs:
            val = entity.get(inp)
            conf = confidence_map.get(inp, 0.0)
            if val is None or conf < edge.confidence_floor:
                missing_for_edge.append(inp)

        if len(missing_for_edge) == 1:
            # This one field is the sole blocker — high unlock value
            blocker = missing_for_edge[0]
            unlock[blocker] = unlock.get(blocker, 0.0) + 1.0
        elif len(missing_for_edge) == 2:
            # Two fields missing — each gets partial credit
            for f in missing_for_edge:
                unlock[f] = unlock.get(f, 0.0) + 0.5

    return unlock


# ──────────────────────────────────────────────
# Target selection: use unlock values to prioritize
# ──────────────────────────────────────────────


def prioritize_search_targets(
    missing_fields: list[str],
    unlock_map: dict[str, float],
    field_map: dict[str, Any] | None = None,
) -> list[str]:
    """Sort missing fields by inference unlock value (descending).

    Fields that unblock the most downstream derivations get searched first.
    This is the bridge between inference and search_optimizer_v2.resolve().
    """

    def sort_key(f: str) -> float:
        return unlock_map.get(f, 0.0)

    return sorted(missing_fields, key=sort_key, reverse=True)


# ──────────────────────────────────────────────
# YAML Contract (additions to domain_spec.yaml)
# ──────────────────────────────────────────────
#
# ontology:
#   nodes:
#     Facility:
#       properties:
#         # Searchable field (input to derivations)
#         polymers_handled:
#           type: list
#           examples: [HDPE, PP, PET]
#
#         certifications:
#           type: list
#           examples: [ISO 9001, R2]
#
#         # Derived field — inference fires ONLY when ALL inputs present
#         material_grade:
#           type: string
#           managed_by: computed
#           derived_from: [polymers_handled, certifications]
#           inference_rule: material_grade_lookup    # registered rule name
#           confidence_floor: 0.6                    # min confidence on inputs
#
#         # Multi-hop: facility_tier derives from fields that are themselves derived
#         facility_tier:
#           type: string
#           managed_by: inference
#           derived_from: [certifications, equipment_types, material_grade]
#           inference_rule: facility_tier_compute
#           confidence_floor: 0.7
#
# The rule functions (material_grade_lookup, facility_tier_compute) are
# registered at startup via register_rule(). They contain the actual
# domain logic (lookup tables, formulas, etc.). The YAML declares the
# graph structure; Python plugins supply the computation.
#
# CRITICAL INVARIANTS:
# - No inference fires with partial inputs. Ever.
# - No cross-entity inference. Each entity is independent.
# - Confidence is bounded by the weakest input. Cannot exceed it.
# - derived_from is the ONLY source of truth for what qualifies as an input.
# - Fields NOT in derived_from have ZERO influence on the derivation,
#   regardless of correlation, co-occurrence, or similarity.
