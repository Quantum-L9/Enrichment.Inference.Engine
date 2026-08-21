"""
Inference Rule Loader — domain-agnostic YAML rule loading + registry.

Loads inference rules from domain YAML files, validates syntax at load time,
builds an indexed registry for O(1) candidate lookup by trigger field.

New in ROI 4: Adds unlock-aware targeting functions that rank missing fields
by how many downstream inference rules they would unlock if populated.
"""

from __future__ import annotations

from collections import defaultdict
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import structlog
import yaml
from pydantic import BaseModel, Field, field_validator

logger = structlog.get_logger("inference.rule_loader")

__all__ = [
    "Operator",
    "RuleCondition",
    "RuleOutput",
    "RuleDefinition",
    "RuleRegistry",
    "load_rules",
    "load_rules_data",
    "reload_rules",
    "build_unlock_index",
    "score_unlock_potential",
    "rank_fields_by_unlock",
]


# ═══════════════════════════════════════════════════════════════════════════
# Operator Enum
# ═══════════════════════════════════════════════════════════════════════════


class Operator(StrEnum):
    """Supported condition operators for inference rules."""

    CONTAINS = "CONTAINS"
    EQUALS = "EQUALS"
    GT = "GT"
    LT = "LT"
    GTE = "GTE"
    LTE = "LTE"
    IN = "IN"
    NOT_IN = "NOTIN"
    IS_TRUE = "ISTRUE"
    IS_FALSE = "ISFALSE"
    EXISTS = "EXISTS"


# ═══════════════════════════════════════════════════════════════════════════
# Rule Definition Models
# ═══════════════════════════════════════════════════════════════════════════


class RuleCondition(BaseModel):
    """Single condition in an inference rule."""

    field: str
    operator: Operator
    value: Any | None = None


class RuleOutput(BaseModel):
    """Single output field assignment in an inference rule."""

    field: str
    value_expr: Any
    derivation_type: Literal["classification", "computation", "lookup", "transform"]


class RuleDefinition(BaseModel):
    """
    Deterministic inference rule loaded from domain YAML.

    Example YAML:
    ```yaml
    inference_rules:
      - rule_id: grade-assignment-hdpe
        conditions:
          - field: materials_handled
            operator: CONTAINS
            value: HDPE
          - field: contamination_tolerance_pct
            operator: LT
            value: 0.05
        outputs:
          - field: material_grade
            value_expr: B
            derivation_type: classification
        confidence: 0.90
        priority: 10
        domain: plastics
    ```
    """

    rule_id: str
    conditions: list[RuleCondition]
    outputs: list[RuleOutput]
    confidence: float = Field(ge=0.0, le=1.0)
    priority: int = Field(default=10, ge=0, le=100)
    domain: str | None = None
    description: str | None = None

    @property
    def trigger_fields(self) -> set[str]:
        """Set of field names that must exist for this rule to potentially fire."""
        return {cond.field for cond in self.conditions}

    @field_validator("conditions")
    @classmethod
    def validate_conditions(cls, v):
        if not v:
            raise ValueError("Rule must have at least one condition")
        return v

    @field_validator("outputs")
    @classmethod
    def validate_outputs(cls, v):
        if not v:
            raise ValueError("Rule must have at least one output")
        return v


# ═══════════════════════════════════════════════════════════════════════════
# Rule Registry
# ═══════════════════════════════════════════════════════════════════════════


class RuleRegistry:
    """
    Indexed registry of inference rules for O(1) candidate lookup.

    Maintains two indices:
    - trigger_index: field_name → list[rule_id] (which rules fire when field exists)
    - rule_index: rule_id → RuleDefinition
    """

    def __init__(self):
        self.trigger_index: dict[str, list[str]] = defaultdict(list)
        self.rule_index: dict[str, RuleDefinition] = {}

    def add_rule(self, rule: RuleDefinition) -> None:
        """Add rule to registry and update trigger index."""
        self.rule_index[rule.rule_id] = rule
        for condition in rule.conditions:
            self.trigger_index[condition.field].append(rule.rule_id)

    def get(self, rule_id: str) -> RuleDefinition | None:
        """Get rule by ID."""
        return self.rule_index.get(rule_id)

    def all_rules(self) -> list[RuleDefinition]:
        """Get all rules in registry."""
        return list(self.rule_index.values())

    def rules_for_field(self, field_name: str) -> list[RuleDefinition]:
        """Get all rules triggered by a specific field."""
        rule_ids = self.trigger_index.get(field_name, [])
        return [self.rule_index[rid] for rid in rule_ids if rid in self.rule_index]

    def count(self) -> int:
        """Total number of rules in registry."""
        return len(self.rule_index)

    def candidates_for(self, available_fields: set[str]) -> list[RuleDefinition]:
        """Return rules whose trigger fields are a subset of available_fields."""
        candidates: list[RuleDefinition] = []
        for rule in self.rule_index.values():
            if rule.trigger_fields.issubset(available_fields):
                candidates.append(rule)
        return candidates

    def clear(self) -> None:
        """Clear all rules (for hot reload)."""
        self.trigger_index.clear()
        self.rule_index.clear()


# ═══════════════════════════════════════════════════════════════════════════
# YAML Loader
# ═══════════════════════════════════════════════════════════════════════════


def load_rules(yaml_path: str | Path) -> RuleRegistry:
    """
    Load inference rules from domain YAML file.

    Args:
        yaml_path: Path to domain YAML containing `inference_rules` section

    Returns:
        Populated RuleRegistry

    Raises:
        FileNotFoundError: If YAML file doesn't exist
        ValueError: If YAML structure is invalid or rules fail validation
    """
    yaml_path = Path(yaml_path)
    if not yaml_path.exists():
        raise FileNotFoundError(f"Domain YAML not found: {yaml_path}")

    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    if not data:
        raise ValueError(f"Empty YAML file: {yaml_path}")

    rules_data = data.get("inference_rules", [])
    if not isinstance(rules_data, list):
        raise ValueError(f"inference_rules must be a list, got {type(rules_data).__name__}")

    registry = load_rules_data(rules_data)
    logger.info(
        "rules_loaded",
        yaml_path=str(yaml_path),
        loaded=registry.count(),
        total=len(rules_data),
    )
    return registry


def load_rules_data(rules_data: list[dict[str, Any]]) -> RuleRegistry:
    """Validate inline inference_rules and build a RuleRegistry."""
    if not isinstance(rules_data, list):
        raise ValueError(f"inference_rules must be a list, got {type(rules_data).__name__}")

    registry = RuleRegistry()
    loaded_count = 0
    error_count = 0

    for idx, rule_dict in enumerate(rules_data):
        if not isinstance(rule_dict, dict):
            error_count += 1
            logger.warning("rule_validation_failed", rule_index=idx, error="rule is not a mapping")
            continue
        try:
            rule = RuleDefinition.model_validate(rule_dict)
            registry.add_rule(rule)
            loaded_count += 1
        except Exception as exc:
            error_count += 1
            logger.warning(
                "rule_validation_failed",
                rule_index=idx,
                rule_id=rule_dict.get("rule_id", "unknown"),
                error=str(exc),
            )

    logger.info("rules_data_loaded", loaded=loaded_count, errors=error_count, total=len(rules_data))
    if error_count > 0 and loaded_count == 0:
        raise ValueError("All rules failed validation")
    return registry


def reload_rules(yaml_path: str | Path, registry: RuleRegistry) -> None:
    """
    Hot-reload rules from YAML, replacing existing registry atomically.

    Args:
        yaml_path: Path to domain YAML
        registry: Existing registry to update in-place
    """
    new_registry = load_rules(yaml_path)
    registry.clear()
    for rule in new_registry.all_rules():
        registry.add_rule(rule)

    logger.info(
        "rules_reloaded",
        yaml_path=str(yaml_path),
        rule_count=registry.count(),
    )


# ═══════════════════════════════════════════════════════════════════════════
# ROI 4: Inference Unlock Targeting
# ═══════════════════════════════════════════════════════════════════════════


def build_unlock_index(registry: RuleRegistry) -> dict[str, list[str]]:
    """
    Build inverse index: field_name → list of rule_ids that would fire if field populated.

    For each rule, maps every input field (condition) to that rule's ID.
    Used by meta_prompt_planner to rank missing fields by inference unlock potential.

    Example:
        If rule "grade-hdpe" has conditions on ["materials_handled", "contamination_tolerance"],
        both fields map to ["grade-hdpe"] in the unlock index.

    Args:
        registry: Loaded rule registry

    Returns:
        Dict mapping field names to rule IDs they unlock
    """
    index: dict[str, list[str]] = defaultdict(list)
    for rule in registry.all_rules():
        for condition in rule.conditions:
            if condition.field not in index[condition.field]:
                index[condition.field].append(rule.rule_id)
    return dict(index)


def score_unlock_potential(
    missing_fields: list[str],
    unlock_index: dict[str, list[str]],
    registry: RuleRegistry,
    domain_spec: Any,  # DomainSpec from domain_yaml_reader
) -> dict[str, float]:
    """
    Score each missing field by total downstream value unlocked if enriched.

    Computes sum of scoring_weights of all OUTPUT fields across all rules
    the missing field would unlock. Gate-critical outputs get 10x multiplier.

    Args:
        missing_fields: List of field names with NULL/missing values
        unlock_index: Output of build_unlock_index()
        registry: Rule registry
        domain_spec: Domain YAML spec with ontology.nodes.properties

    Returns:
        Dict mapping field name → unlock score (higher = more valuable to enrich)
    """
    scores: dict[str, float] = {}

    # Build property lookup map
    prop_map: dict[str, Any] = {}
    if hasattr(domain_spec, "ontology") and hasattr(domain_spec.ontology, "nodes"):
        for node in domain_spec.ontology.nodes:
            if hasattr(node, "properties"):
                for prop in node.properties:
                    prop_map[prop.name] = prop

    for field_name in missing_fields:
        rule_ids = unlock_index.get(field_name, [])
        total_value = 0.0

        for rule_id in rule_ids:
            rule = registry.get(rule_id)
            if not rule:
                continue

            for output in rule.outputs:
                prop = prop_map.get(output.field)
                if not prop:
                    continue

                # Scoring weight from domain YAML (default 0.0)
                weight = 0.0
                if hasattr(prop, "metadata") and isinstance(prop.metadata, dict):
                    weight = prop.metadata.get("scoring_weight", 0.0)

                # Gate-critical multiplier
                multiplier = 1.0
                if (
                    hasattr(prop, "metadata")
                    and isinstance(prop.metadata, dict)
                    and prop.metadata.get("gate_critical")
                ):
                    multiplier = 10.0

                total_value += weight * multiplier * rule.confidence

        scores[field_name] = total_value

    return scores


def rank_fields_by_unlock(
    missing_fields: list[Any],  # list[FieldHealth] from field_analyzer
    unlock_index: dict[str, list[str]],
    registry: RuleRegistry,
    domain_spec: Any,
) -> list[Any]:
    """
    Rank missing fields by combined priority:
      1. Gate-critical fields (1000 base priority)
      2. Inference unlock potential (derived-field value)
      3. Direct scoring weight (field's own contribution)

    Args:
        missing_fields: FieldHealth objects or dicts with field_name, is_gate_critical, scoring_weight
        unlock_index: Output of build_unlock_index()
        registry: Rule registry
        domain_spec: Domain YAML spec

    Returns:
        Sorted list (highest priority first)
    """
    field_names = []
    for f in missing_fields:
        if hasattr(f, "field_name"):
            field_names.append(f.field_name)
        elif isinstance(f, dict) and "field_name" in f:
            field_names.append(f["field_name"])
        else:
            field_names.append(str(f))

    unlock_scores = score_unlock_potential(field_names, unlock_index, registry, domain_spec)

    def priority_key(field: Any) -> float:
        # Extract field attributes
        if hasattr(field, "field_name"):
            fname = field.field_name
            is_gate = getattr(field, "is_gate_critical", False)
            weight = getattr(field, "scoring_weight", 0.0)
        elif isinstance(field, dict):
            fname = field.get("field_name", str(field))
            is_gate = field.get("is_gate_critical", False)
            weight = field.get("scoring_weight", 0.0)
        else:
            fname = str(field)
            is_gate = False
            weight = 0.0

        gate_priority = 1000.0 if is_gate else 0.0
        unlock_value = unlock_scores.get(fname, 0.0)
        direct_value = weight

        return gate_priority + unlock_value + direct_value

    return sorted(missing_fields, key=priority_key, reverse=True)
