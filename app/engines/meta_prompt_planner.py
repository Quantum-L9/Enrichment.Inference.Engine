"""
Meta-Prompt Planner — adaptive prompt strategy per convergence pass.

Analyzes entity state (field coverage, confidence, uncertainty) and determines:
- Which enrichment mode: discovery / targeted / verification
- Which fields to prioritize in the prompt
- Which KB fragments to inject
- Optimal variation budget (1-5 variations)

ROI 4: Uses inference-unlock-aware field ranking from rule_loader to maximize
derived-field output per Sonar token spent.
"""

from __future__ import annotations

from typing import Any, Literal

import structlog

from ..models.loop_schemas import PassContext
from ..services.domain_yaml_reader import DomainSpec

logger = structlog.get_logger("meta_prompt_planner")


# ═══════════════════════════════════════════════════════════════════════════
# Planner Core
# ═══════════════════════════════════════════════════════════════════════════


class MetaPromptPlanner:
    """
    Adaptive prompt strategy planner for convergence loop.

    Determines enrichment mode, field priorities, KB context, and variation
    budget based on entity state and uncertainty metrics.
    """

    def __init__(
        self,
        domain_spec: DomainSpec | None = None,
        rule_registry: Any = None,  # RuleRegistry from inference.rule_loader
        unlock_index: dict[str, list[str]] | None = None,
    ):
        """
        Args:
            domain_spec: Domain YAML specification (optional)
            rule_registry: Optional rule registry for unlock-aware targeting
            unlock_index: Optional pre-built unlock index (output of build_unlock_index)
        """
        self.domain_spec = domain_spec
        self.rule_registry = rule_registry
        self.unlock_index = unlock_index

    def plan(
        self,
        entity: dict[str, Any],
        known_fields: dict[str, float],
        _inferred_fields: dict[str, Any],
        domain_hints: dict[str, Any],
        _inference_rule_catalog: list[Any],
        pass_number: int,
    ) -> PromptPlan:
        """
        Simplified plan method for test compatibility.

        Args:
            entity: Entity data
            known_fields: Field name -> confidence mapping
            inferred_fields: Inferred field values
            domain_hints: Domain-specific hints
            inference_rule_catalog: Available inference rules
            pass_number: Current pass number

        Returns:
            PromptPlan with mode, priority fields, KB fragments, variation budget
        """
        mode = self._determine_mode(pass_number, uncertainty_score=5.0)
        priority_fields = domain_hints.get("priority_fields", [])
        if not priority_fields:
            # Identify missing fields from entity
            all_fields = set(entity.keys()) | set(known_fields.keys())
            priority_fields = [f for f in all_fields if known_fields.get(f, 0) < 0.8]

        return PromptPlan(
            mode=mode,
            priority_fields=priority_fields[:10],
            kb_fragment_ids=[],
            variation_count=3,
            pass_number=pass_number,
            reasoning=f"Pass {pass_number}: {mode} mode",
        )

    def plan_pass(
        self,
        entity_state: dict[str, Any],
        pass_context: PassContext,
        field_confidences: dict[str, float],
        uncertainty_score: float,
    ) -> PromptPlan:
        """
        Plan enrichment strategy for next convergence pass.

        Args:
            entity_state: Current entity field values
            pass_context: Pass number, budget remaining, previous results
            field_confidences: Per-field confidence scores (0.0-1.0)
            uncertainty_score: Aggregate uncertainty metric

        Returns:
            PromptPlan with mode, priority fields, KB fragments, variation budget
        """
        mode = self._determine_mode(pass_context.pass_number, uncertainty_score)
        missing_fields = self._identify_missing_fields(entity_state, field_confidences)
        priority_fields = self._rank_fields(missing_fields, mode)
        kb_fragments = self._select_kb_fragments(priority_fields, mode)
        variation_budget = self._compute_variation_budget(
            uncertainty_score, pass_context.budget_remaining
        )

        plan = PromptPlan(
            mode=mode,
            priority_fields=priority_fields[:10],  # Top 10 max
            kb_fragment_ids=kb_fragments,
            variation_count=variation_budget,
            pass_number=pass_context.pass_number,
            reasoning=self._generate_reasoning(mode, priority_fields, uncertainty_score),
        )

        logger.info(
            "prompt_plan_generated",
            mode=mode,
            priority_fields=len(priority_fields),
            variations=variation_budget,
            uncertainty=round(uncertainty_score, 3),
            pass_number=pass_context.pass_number,
        )

        return plan

    # ─────────────────────────────────────────────────────────────────────────
    # Internal Logic
    # ─────────────────────────────────────────────────────────────────────────

    def _determine_mode(
        self, pass_number: int, uncertainty_score: float
    ) -> Literal["discovery", "targeted", "verification"]:
        """
        Select enrichment mode based on pass number and uncertainty.

        - Pass 1: Always discovery (broad research)
        - Pass 2+: Targeted if uncertainty > 2.0, else verification
        """
        if pass_number == 1:
            return "discovery"
        elif uncertainty_score > 2.0:
            return "targeted"
        else:
            return "verification"

    def _identify_missing_fields(
        self, entity_state: dict[str, Any], field_confidences: dict[str, float]
    ) -> list[FieldGap]:
        """
        Identify fields that are NULL or low-confidence.

        Returns list of FieldGap objects with field name, current value, confidence.
        """
        gaps: list[FieldGap] = []
        for field_name, value in entity_state.items():
            confidence = field_confidences.get(field_name, 0.0)
            is_missing = value is None or value == "" or confidence < 0.7

            if is_missing:
                gaps.append(
                    FieldGap(
                        field_name=field_name,
                        current_value=value,
                        confidence=confidence,
                        is_gate_critical=self._is_gate_critical(field_name),
                        scoring_weight=self._get_scoring_weight(field_name),
                    )
                )

        return gaps

    def _rank_fields(self, missing_fields: list[FieldGap], mode: str) -> list[str]:
        """
        Rank missing fields by priority for enrichment.

        ROI 4: Uses inference-unlock-aware ranking when rule_registry available.
        Fallback: Gate-critical → scoring weight → alphabetical.

        Args:
            missing_fields: List of FieldGap objects
            mode: Enrichment mode (discovery/targeted/verification)

        Returns:
            Ordered list of field names (highest priority first)
        """
        if mode == "discovery":
            # Discovery mode: research all gaps broadly
            return [f.field_name for f in missing_fields]

        # Targeted/verification: use unlock-aware ranking if available
        if self.rule_registry and self.unlock_index:
            try:
                from ..engines.inference.rule_loader import rank_fields_by_unlock

                ranked = rank_fields_by_unlock(
                    missing_fields,
                    self.unlock_index,
                    self.rule_registry,
                    self.domain_spec,
                )
                return [
                    f.field_name if hasattr(f, "field_name") else f["field_name"] for f in ranked
                ]
            except Exception as exc:
                logger.warning(
                    "unlock_ranking_failed",
                    error=str(exc),
                    fallback="scoring_weight",
                )

        # Fallback: traditional ranking by gate + weight
        ranked = sorted(
            missing_fields,
            key=lambda f: (
                1000 if f.is_gate_critical else 0,
                f.scoring_weight,
                f.field_name,
            ),
            reverse=True,
        )
        return [f.field_name for f in ranked]

    def _select_kb_fragments(self, priority_fields: list[str], mode: str) -> list[str]:
        """
        Select KB fragment IDs relevant to priority fields.

        Returns max 3 KB fragment IDs for context injection.
        """
        # Placeholder: Would query KB resolver with priority fields
        # For now, return empty list (KB injection happens in enrichment_orchestrator)
        return []

    def _compute_variation_budget(self, uncertainty_score: float, budget_remaining: int) -> int:
        """
        Compute optimal Sonar variation count based on uncertainty and budget.

        - High uncertainty (> 5.0): 5 variations
        - Medium uncertainty (2.0-5.0): 3 variations
        - Low uncertainty (< 2.0): 2 variations
        - Budget exhausted: 1 variation minimum
        """
        if budget_remaining < 1000:
            return 1

        if uncertainty_score > 5.0:
            return 5
        elif uncertainty_score > 2.0:
            return 3
        else:
            return 2

    def _is_gate_critical(self, field_name: str) -> bool:
        """Check if field is gate-critical in domain spec."""
        if not hasattr(self.domain_spec, "ontology"):
            return False

        for node in self.domain_spec.ontology.nodes:
            if not hasattr(node, "properties"):
                continue
            for prop in node.properties:
                if (
                    prop.name == field_name
                    and hasattr(prop, "metadata")
                    and isinstance(prop.metadata, dict)
                ):
                    return bool(prop.metadata.get("gate_critical", False))
        return False

    def _get_scoring_weight(self, field_name: str) -> float:
        """Get scoring weight for field from domain spec."""
        if not hasattr(self.domain_spec, "ontology"):
            return 0.0

        for node in self.domain_spec.ontology.nodes:
            if not hasattr(node, "properties"):
                continue
            for prop in node.properties:
                if (
                    prop.name == field_name
                    and hasattr(prop, "metadata")
                    and isinstance(prop.metadata, dict)
                ):
                    return float(prop.metadata.get("scoring_weight", 0.0))
        return 0.0

    def _generate_reasoning(
        self, mode: str, priority_fields: list[str], uncertainty_score: float
    ) -> str:
        """Generate human-readable reasoning for this plan."""
        top_fields = ", ".join(priority_fields[:5])
        return f"Mode: {mode} | Uncertainty: {uncertainty_score:.2f} | Top fields: {top_fields}"


# ═══════════════════════════════════════════════════════════════════════════
# Plan Schema
# ═══════════════════════════════════════════════════════════════════════════


class FieldGap:
    """Represents a missing or low-confidence field."""

    def __init__(
        self,
        field_name: str,
        current_value: Any,
        confidence: float,
        is_gate_critical: bool = False,
        scoring_weight: float = 0.0,
    ):
        self.field_name = field_name
        self.current_value = current_value
        self.confidence = confidence
        self.is_gate_critical = is_gate_critical
        self.scoring_weight = scoring_weight


class PromptPlan:
    """
    Enrichment strategy for a single convergence pass.

    Specifies mode, priority fields, KB context, and variation budget.
    """

    def __init__(
        self,
        mode: Literal["discovery", "targeted", "verification"],
        priority_fields: list[str],
        kb_fragment_ids: list[str],
        variation_count: int,
        pass_number: int,
        reasoning: str = "",
    ):
        self.mode = mode
        self.priority_fields = priority_fields
        self.kb_fragment_ids = kb_fragment_ids
        self.variation_count = variation_count
        self.pass_number = pass_number
        self.reasoning = reasoning

    @property
    def objective(self) -> str:
        """Enrichment objective based on mode."""
        if self.mode == "discovery":
            return "Discover new fields and populate entity data"
        elif self.mode == "targeted":
            return f"Target specific fields: {', '.join(self.priority_fields[:3])}"
        else:
            return "Verify and validate existing field values"

    @property
    def target_fields(self) -> list[str]:
        """Alias for priority_fields for test compatibility."""
        return self.priority_fields

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for logging/telemetry."""
        return {
            "mode": self.mode,
            "priority_fields": self.priority_fields,
            "kb_fragment_ids": self.kb_fragment_ids,
            "variation_count": self.variation_count,
            "pass_number": self.pass_number,
            "reasoning": self.reasoning,
            "objective": self.objective,
        }


# Alias for backward compatibility with tests
SearchPlan = PromptPlan
