"""
Inference Unlock Scorer — computes enrichment targeting based on rule dependencies.

Core insight: not all missing fields are equal. A field that unlocks 5 inference rules
(enabling derived fields like materialgrade, facilitytier, applicationclass) has
10-100x more value than a field with the same scoring_weight that unlocks zero rules.

The unlock index is a DAG: field → rules → output_fields → downstream_rules.
This module walks that graph to compute per-field unlock potential.

Used by meta_prompt_planner.py to rank Pass 2+ enrichment targets.
"""

from __future__ import annotations

from app.utils.safe_convert import safe_float
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from .inference.rule_loader import RuleRegistry

from typing import Any

# DomainSpec promoted to Any until schemas.py exposes the type
DomainSpec = Any

logger = structlog.get_logger("inference_unlock_scorer")


@dataclass
class UnlockScore:
    """
    Per-field unlock potential.

    Attributes:
        field_name: field being scored
        rules_unlocked: count of rules that would fire if field populated
        derived_fields: output fields produced by unlocked rules
        total_weight: sum of scoring_weights of all derived fields
        gate_critical_derived: count of gate-critical fields in derived set
        unlock_score: composite score (gate_critical_derived * 1000 + total_weight)
    """

    field_name: str
    rules_unlocked: int
    derived_fields: set[str]
    total_weight: float
    gate_critical_derived: int
    unlock_score: float


def build_unlock_index(registry: RuleRegistry) -> dict[str, list[str]]:
    """
    Build field → [rule_ids] mapping for O(1) unlock lookup.

    For each rule, extract all input fields from conditions and map them
    to the rule_id. Result: given a field name, instantly retrieve all rules
    where that field appears as a condition trigger.

    Args:
        registry: loaded rule registry

    Returns:
        dict mapping field_name → list of rule_ids that would fire if field populated
    """
    index: dict[str, list[str]] = {}
    for rule in registry.all_rules():
        for condition in rule.conditions:
            field_name = condition.field
            if field_name not in index:
                index[field_name] = []
            if rule.rule_id not in index[field_name]:
                index[field_name].append(rule.rule_id)
    return index


def compute_unlock_scores(
    missing_fields: list[str],
    unlock_index: dict[str, list[str]],
    registry: RuleRegistry,
    domain_spec: DomainSpec,
) -> dict[str, UnlockScore]:
    """
    Compute unlock potential for each missing field.

    Algorithm:
    1. For each missing field, retrieve rule_ids from unlock_index
    2. For each rule, sum the scoring_weight of all OUTPUT fields
    3. Apply 1000x multiplier for gate-critical outputs (they unblock matching entirely)
    4. Return ranked dict

    Args:
        missing_fields: list of field names not yet populated
        unlock_index: result from build_unlock_index()
        registry: rule registry for output field lookup
        domain_spec: domain YAML spec for property metadata

    Returns:
        dict mapping field_name → UnlockScore
    """
    # Build property lookup for metadata access
    prop_map: dict[str, dict] = {}
    for node in domain_spec.ontology.nodes:
        for prop in node.properties:
            prop_map[prop.name] = {
                "scoring_weight": prop.metadata.get("scoring_weight", 0.0),
                "gate_critical": prop.metadata.get("gate_critical", False),
            }

    scores: dict[str, UnlockScore] = {}

    for field_name in missing_fields:
        rule_ids = unlock_index.get(field_name, [])
        derived_fields: set[str] = set()
        total_weight = 0.0
        gate_critical_count = 0

        for rule_id in rule_ids:
            rule = registry.get(rule_id)
            if not rule:
                continue

            for output in rule.outputs:
                output_field = output.field
                derived_fields.add(output_field)

                prop_meta = prop_map.get(output_field, {})
                weight = prop_meta.get("scoring_weight", 0.0)
                is_gate_critical = prop_meta.get("gate_critical", False)

                if is_gate_critical:
                    gate_critical_count += 1
                    total_weight += weight * 1000  # gate-critical multiplier
                else:
                    total_weight += weight

        # Composite score: gate-critical derived fields dominate, then total weight
        unlock_score_val = (gate_critical_count * 1000) + total_weight

        scores[field_name] = UnlockScore(
            field_name=field_name,
            rules_unlocked=len(rule_ids),
            derived_fields=derived_fields,
            total_weight=total_weight,
            gate_critical_derived=gate_critical_count,
            unlock_score=unlock_score_val,
        )

    return scores


def score_and_rank_fields(
    missing_fields: list[dict],
    unlock_index: dict[str, list[str]],
    registry: RuleRegistry,
    domain_spec: DomainSpec,
) -> list[dict]:
    """
    Rank missing fields by combined priority:
    1. Gate-critical fields (1000 base)
    2. Inference unlock potential (derived-field value)
    3. Direct scoring weight (field's own contribution)

    Args:
        missing_fields: list of dicts with keys 'field_name', 'is_gate_critical', 'scoring_weight'
        unlock_index: result from build_unlock_index()
        registry: rule registry
        domain_spec: domain YAML spec

    Returns:
        sorted list of missing_fields dicts, highest priority first
    """
    field_names = [f["field_name"] for f in missing_fields]
    unlock_scores = compute_unlock_scores(field_names, unlock_index, registry, domain_spec)

    def priority_key(field_dict: dict) -> float:
        field_name = field_dict["field_name"]
        is_gate_critical = field_dict.get("is_gate_critical", False)
        scoring_weight = safe_float(field_dict.get("scoring_weight", 0.0))
        unlock_score_obj = unlock_scores.get(field_name)

        base = 1000 if is_gate_critical else 0
        unlock_contrib = unlock_score_obj.unlock_score if unlock_score_obj else 0.0
        return base + unlock_contrib + scoring_weight

    ranked = sorted(missing_fields, key=priority_key, reverse=True)

    # Log top 3 for observability
    if ranked:
        top_field = ranked[0]["field_name"]
        unlock_obj = unlock_scores.get(top_field)
        if unlock_obj:
            logger.info(
                "field_ranking_top",
                field=top_field,
                rules_unlocked=unlock_obj.rules_unlocked,
                derived_fields=len(unlock_obj.derived_fields),
                unlock_score=round(unlock_obj.unlock_score, 2),
            )

    return ranked


def get_top_unlock_metadata(
    ranked_fields: list[dict],
    unlock_index: dict[str, list[str]],
    registry: RuleRegistry,
    domain_spec: DomainSpec,
) -> tuple[str | None, float]:
    """
    Extract top field and its unlock score for telemetry.

    Returns:
        (top_field_name, unlock_score) or (None, 0.0) if no fields
    """
    if not ranked_fields:
        return None, 0.0

    top_field_name = ranked_fields[0]["field_name"]
    unlock_scores = compute_unlock_scores([top_field_name], unlock_index, registry, domain_spec)
    unlock_obj = unlock_scores.get(top_field_name)

    if unlock_obj:
        return top_field_name, unlock_obj.unlock_score
    return top_field_name, 0.0
