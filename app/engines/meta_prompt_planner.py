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

from collections.abc import Collection, Mapping, Sequence
from typing import Any, Literal

import structlog

from app.utils.safe_convert import safe_float

from ..models.loop_schemas import PassContext
from ..services.domain_yaml_reader import DomainSpec
from .convergence.acquisition_order import evidence_tiebreak
from .convergence.confidence_tracker import normalize_confidence, value_present

logger = structlog.get_logger("meta_prompt_planner")


def _named_property(properties: Any, field_name: str) -> Mapping[str, Any] | None:
    """Return one property spec from a mapping or the canonical YAML list."""
    if isinstance(properties, Mapping):
        property_spec = properties.get(field_name)
        if isinstance(property_spec, Mapping):
            return property_spec
        return None
    if isinstance(properties, list):
        for item in properties:
            if isinstance(item, Mapping) and item.get("name") == field_name:
                return item
    return None


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
        domain_spec: Mapping[str, Any] | DomainSpec | None = None,
        rule_registry: Any = None,  # RuleRegistry from inference.rule_loader
        unlock_index: dict[str, list[str]] | None = None,
        confidence_threshold: float = 0.7,
        priority_fields: list[str] | None = None,
        uncertainty_threshold: float = 2.0,
    ):
        """
        Args:
            domain_spec: Raw Domain YAML mapping (canonical), or a legacy
                ``DomainSpec`` object without ontology metadata (optional)
            rule_registry: Optional rule registry for unlock-aware targeting
            unlock_index: Optional pre-built unlock index (output of build_unlock_index)
            confidence_threshold: A field below this confidence is a gap to
                re-target (F-017). The convergence loop passes its configured
                threshold; 0.7 is only the standalone default.
            priority_fields: Domain hint fields (enrichment_hints.priority_fields)
                that are targeted first, in the order given (F-019).
        """
        self.domain_spec = domain_spec
        self.rule_registry = rule_registry
        self.unlock_index = unlock_index
        self.confidence_threshold = confidence_threshold
        self.priority_fields = list(priority_fields or [])
        self.uncertainty_threshold = uncertainty_threshold

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
            # Identify missing fields from entity, at the planner's own bar.
            all_fields = set(entity.keys()) | set(known_fields.keys())
            priority_fields = sorted(
                f
                for f in all_fields
                if normalize_confidence(known_fields.get(f)) < self.confidence_threshold
            )

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
        required_fields: Sequence[str] | None = None,
        field_thresholds: Mapping[str, float] | None = None,
        deciding_fields: Collection[str] | None = None,
        withheld_fields: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> PromptPlan:
        """
        Plan enrichment strategy for next convergence pass.

        Args:
            entity_state: Current entity field values
            pass_context: Pass number, budget remaining, previous results
            field_confidences: Per-field confidence scores (0.0-1.0)
            uncertainty_score: Operational uncertainty U_action (D-9 v2): the
                weakest required field's deficit. It sets the pass-level
                variation budget and, when no field is unresolved, the mode.
            required_fields: The caller's explicit schema, if any. Each such
                field is a target until it is ready, even when the entity does
                not carry it yet, and leads the target list.
            field_thresholds: Consumer per-field readiness bars. A field without
                an entry uses the planner's confidence threshold.
            deciding_fields: Without a schema, the tracked fields that decide
                convergence (inference-derived values excluded). Any of them
                still unresolved keeps the pass targeted.
            withheld_fields: What valid replies said about fields earlier passes
                did not admit. Used only to order gap fields the ranking below
                leaves equal (`evidence_tiebreak`); it changes no mode, budget,
                variation count or threshold. None leaves the order as it was.

        Returns:
            PromptPlan with mode, priority fields, KB fragments, variation budget
        """
        required = list(dict.fromkeys(required_fields or []))
        thresholds = dict(field_thresholds or {})
        missing_fields = self._identify_missing_fields(
            entity_state,
            field_confidences,
            required_fields=required,
            field_thresholds=thresholds,
        )
        gap_names = {gap.field_name for gap in missing_fields}
        has_required_gaps = any(name in gap_names for name in required)
        has_deciding_gaps = not required and any(
            name in gap_names for name in (deciding_fields or ())
        )
        mode = self._determine_mode(
            pass_context.pass_number,
            uncertainty_score,
            has_required_gaps=has_required_gaps,
            has_deciding_gaps=has_deciding_gaps,
        )
        tiebreak = evidence_tiebreak(gap_names, withheld_fields or {})
        priority_fields = self._required_first(
            self._hints_first(self._rank_fields(missing_fields, mode, tiebreak)), required
        )
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
        self,
        pass_number: int,
        uncertainty_score: float,
        *,
        has_required_gaps: bool = False,
        has_deciding_gaps: bool = False,
    ) -> Literal["discovery", "targeted", "verification"]:
        """
        Select enrichment mode (D-9 v2 precedence).

        - Pass 1: discovery (broad research)
        - Pass 2+, an explicitly required field still unresolved: targeted
        - Pass 2+, no schema, a deciding field still unresolved: targeted
        - Pass 2+, U_action > uncertainty_threshold (2.0): targeted
        - Otherwise: verification

        A field is unresolved when its value is missing or its confidence is
        below its own threshold, so a low U_action cannot hide a stricter
        consumer bar (confidence 0.90 against a 0.95 threshold is U_action 1,
        but the field is still a target).
        """
        if pass_number == 1:
            return "discovery"
        if has_required_gaps or has_deciding_gaps:
            return "targeted"
        if uncertainty_score > self.uncertainty_threshold:
            return "targeted"
        return "verification"

    def _threshold_for(self, field_name: str, field_thresholds: Mapping[str, float]) -> float:
        """A field's readiness bar: its consumer threshold, else the planner's."""
        return field_thresholds.get(field_name, self.confidence_threshold)

    def _identify_missing_fields(
        self,
        entity_state: dict[str, Any],
        field_confidences: dict[str, float],
        required_fields: Sequence[str] = (),
        field_thresholds: Mapping[str, float] | None = None,
    ) -> list[FieldGap]:
        """
        Identify fields that are missing or below their own confidence threshold.

        Candidates are the entity's fields, the domain hint fields and the
        explicitly required fields, so a required or hinted field the entity
        does not carry yet is a gap (F-019). A value of None or "" is missing;
        0 and False are values. Confidence is normalized (absent, non-numeric or
        non-finite counts as 0) and compared with the field's consumer
        threshold, else the planner's.

        Returns list of FieldGap objects with field name, current value, confidence.
        """
        thresholds = field_thresholds or {}
        gaps: list[FieldGap] = []
        candidates = dict(entity_state)
        for named in (*self.priority_fields, *required_fields):
            candidates.setdefault(named, None)
        for field_name, value in candidates.items():
            confidence = normalize_confidence(field_confidences.get(field_name))
            is_missing = not value_present(value) or confidence < self._threshold_for(
                field_name, thresholds
            )

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

    def _rank_fields(
        self,
        missing_fields: list[FieldGap],
        mode: str,
        tiebreak: Mapping[str, int] | None = None,
    ) -> list[str]:
        """
        Rank missing fields by priority for enrichment.

        ROI 4: Uses inference-unlock-aware ranking when rule_registry available.
        Fallback: Gate-critical → scoring weight → evidence tie-break → alphabetical.
        `tiebreak` orders only fields equal on gate and weight; without it, or
        when every field ranks the same, the order is exactly what it was.

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
                (tiebreak or {}).get(f.field_name, 0),
                f.field_name,
            ),
            reverse=True,
        )
        return [f.field_name for f in ranked]

    def _hints_first(self, ranked: list[str]) -> list[str]:
        """Put the domain's priority fields that are gaps first, in hint order."""
        gaps = set(ranked)
        hinted = [name for name in dict.fromkeys(self.priority_fields) if name in gaps]
        return hinted + [name for name in ranked if name not in set(hinted)]

    @staticmethod
    def _required_first(ranked: list[str], required: Sequence[str]) -> list[str]:
        """Unresolved required fields before optional gaps; ranking kept within each.

        The plan keeps at most ten targets, so an optional gap must never push a
        required one out of the list.
        """
        wanted = set(required)
        return [n for n in ranked if n in wanted] + [n for n in ranked if n not in wanted]

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
        Compute the pass-level Sonar variation count from U_action and budget.

        - U_action > 5.0: 5 variations
        - 2.0 < U_action <= 5.0: 3 variations
        - U_action <= 2.0: 2 variations
        - Budget exhausted: 1 variation minimum

        These are planning values: the loop still caps them at the request's
        max_variations, and token budget and deadline still bound the pass.
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
        metadata = self._property_metadata(field_name)
        if metadata is None:
            return False
        return bool(metadata.get("gate_critical", False))

    def _get_scoring_weight(self, field_name: str) -> float:
        """Get scoring weight for field from domain spec."""
        metadata = self._property_metadata(field_name)
        if metadata is None:
            return 0.0
        return safe_float(metadata.get("scoring_weight", 0.0))

    def _property_metadata(self, field_name: str) -> Mapping[str, Any] | None:
        """Return a field's metadata from a raw ontology mapping or YAML list.

        Domain YAML reaches the convergence loop as an unvalidated mapping. A
        malformed or incomplete spec cannot prevent enrichment, so each level
        is checked before it is traversed.
        """
        if not isinstance(self.domain_spec, Mapping):
            return None

        ontology = self.domain_spec.get("ontology")
        if not isinstance(ontology, Mapping):
            return None

        nodes = ontology.get("nodes")
        if isinstance(nodes, Mapping):
            node_values = list(nodes.values())
        elif isinstance(nodes, list):
            node_values = nodes
        else:
            return None

        for node in node_values:
            if not isinstance(node, Mapping):
                continue
            property_spec = _named_property(node.get("properties"), field_name)
            if property_spec is None:
                continue
            metadata = property_spec.get("metadata")
            if isinstance(metadata, Mapping):
                return metadata
            direct = {
                key: property_spec[key]
                for key in ("gate_critical", "scoring_weight")
                if key in property_spec
            }
            if direct:
                return direct
        return None

    def _generate_reasoning(
        self, mode: str, priority_fields: list[str], uncertainty_score: float
    ) -> str:
        """Generate human-readable reasoning for this plan."""
        top_fields = ", ".join(priority_fields[:5])
        return f"Mode: {mode} | U_action: {uncertainty_score:.2f} | Top fields: {top_fields}"


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
