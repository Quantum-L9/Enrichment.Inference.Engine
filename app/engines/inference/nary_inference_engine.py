"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: enrich
layer: [inference]
tags: [inference, n-ary, kb-integration, matching]
owner: engine-team
status: active
--- /L9_META ---

N-ary Native Inference Engine.

Handles inference over qualified n-ary facts as first-class constructs.
Integrates with RuleEngine (binary) for hybrid inference passes.

Fixes applied vs. v1:
  - Inferred confidence clamped to [0,1] (weight*conf could exceed 1.0)
  - Certainty factor model: CF_result = CF_rule * CF_evidence * decay^hops
  - load_kb_facts() deduplicates on (relation, participants_key)
  - match_kb_facts() adds qualifier alignment scoring alongside raw confidence
  - RuleEngine bridge: to_rule_engine_format() produces InferenceResult-compatible
    dicts for combined binary+n-ary scoring in inference_bridge_v2.py
  - Provenance chain included in NAryInferenceResult.qualifier_trace

Consumes:
  app.engines.inference.rule_engine.RuleEngine  (bridge output)
  app.engines.inference_bridge_v2               (orchestration caller)
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from app.utils.safe_convert import safe_float

logger = logging.getLogger(__name__)

# Certainty factor decay per chain hop (mirrors hoprag multi-hop decay convention)
DEFAULT_HOP_DECAY: float = 0.9


@dataclass
class NAryFact:
    """A qualified n-ary fact from the domain KB.

    participants: role → entity_id mapping.
    qualifiers: additional non-entity context properties.
    provenance: source string for audit trail.
    """

    relation: str
    participants: dict[str, str]
    qualifiers: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    provenance: str = ""

    def _participants_key(self) -> str:
        """Deterministic dedup key for participants."""
        return hashlib.sha256(
            (
                f"{self.relation}:"
                + ";".join(f"{k}={v}" for k, v in sorted(self.participants.items()))
            ).encode()
        ).hexdigest()


@dataclass
class NAryInferenceResult:
    """Result of n-ary inference."""

    inferred_relation: str
    participants: dict[str, str]
    confidence: float  # clamped to [0,1]
    supporting_facts: list[NAryFact] = field(default_factory=list)
    rule_name: str = ""
    hop_count: int = 0
    qualifier_trace: dict[str, Any] = field(
        default_factory=dict
    )  # which qualifiers drove inference
    explanation: str = ""

    def to_rule_engine_format(self) -> dict[str, Any]:
        """Bridge to RuleEngine / inference_bridge_v2 expected format.

        Produces a dict compatible with the InferenceResult protocol in
        app.engines.inference.rule_engine so combined binary+n-ary scoring
        passes can treat results uniformly.
        """
        return {
            "relation": self.inferred_relation,
            "entities": list(self.participants.values()),
            "confidence": self.confidence,
            "source": "nary_inference",
            "rule": self.rule_name,
            "provenance": [f.provenance for f in self.supporting_facts],
            "qualifier_trace": self.qualifier_trace,
            "explanation": self.explanation,
        }


@dataclass
class NAryRule:
    """Inference rule over n-ary facts.

    antecedents: list of pattern dicts:
      {"relation": str, "required_roles": [str], "qualifier_constraints": {str: Any}}
    consequent: {"relation": str, "role_mapping": {new_role: src_role}}
    certainty_factor: rule-level weight (0.0–1.0)
    hop_decay: applied per chained antecedent step
    """

    name: str
    antecedents: list[dict[str, Any]]
    consequent: dict[str, Any]
    min_confidence: float = 0.5
    certainty_factor: float = 1.0
    hop_decay: float = DEFAULT_HOP_DECAY
    description: str = ""


def _qualifier_alignment_score(
    query_qualifiers: dict[str, Any],
    fact_qualifiers: dict[str, Any],
) -> float:
    """Score qualifier alignment between query context and a KB fact.

    Returns [0,1]:
      1.0 = all query qualifiers present and value-compatible in fact
      0.0 = no qualifier overlap

    Numeric range qualifiers ("mfi_range": "4.0-6.0") checked for
    overlap, not exact match.
    """
    if not query_qualifiers:
        return 1.0  # no query constraints = full pass

    matched: float = 0
    for k, qv in query_qualifiers.items():
        fv = fact_qualifiers.get(k)
        if fv is None:
            continue
        qv_str, fv_str = str(qv), str(fv)
        # Exact match
        if qv_str == fv_str:
            matched += 1
            continue
        # Numeric range overlap: "4.0-6.0" vs "3.5-5.0"
        if "-" in qv_str and "-" in fv_str:
            try:
                q_lo, q_hi = (float(x) for x in qv_str.split("-"))
                f_lo, f_hi = (float(x) for x in fv_str.split("-"))
                if q_lo <= f_hi and f_lo <= q_hi:  # overlap
                    matched += 1
                continue
            except ValueError:
                pass
        # Partial string match
        if qv_str.lower() in fv_str.lower() or fv_str.lower() in qv_str.lower():
            matched += 0.5

    return safe_float(min(matched / len(query_qualifiers), 1.0))


class NAryInferenceEngine:
    """N-ary native inference engine.

    Lifecycle:
      1. register_rule(rule)
      2. load_kb_facts(facts)  — with deduplication
      3. infer(entity_id, relation_filter) → list[NAryInferenceResult]
      4. match_kb_facts(entity_id, query_qualifiers, top_k) → ranked [(fact, score)]
      5. combined_infer(entity_id, rule_engine_results) → merged results
    """

    def __init__(self) -> None:
        self._rules: list[NAryRule] = []
        self._kb_facts: list[NAryFact] = []
        self._entity_index: dict[str, list[NAryFact]] = {}
        self._dedup_keys: set[str] = set()
        self._register_default_rules()

    def _register_default_rules(self) -> None:
        self._rules = [
            NAryRule(
                name="shared_grade_buyer_path",
                antecedents=[
                    {
                        "relation": "RECYCLES_GRADE_FOR_BUYER",
                        "required_roles": ["facility", "material_grade", "buyer"],
                    }
                ],
                consequent={
                    "relation": "COMPATIBLE_SUPPLIER_FOR_BUYER",
                    "role_mapping": {"supplier": "facility", "buyer": "buyer"},
                },
                min_confidence=0.6,
                certainty_factor=0.9,
                hop_decay=0.9,
                description="Facility that recycles a grade for buyer = compatible supplier",
            ),
            NAryRule(
                name="tier_grade_compatibility",
                antecedents=[
                    {
                        "relation": "PROCESSES_AT_TIER",
                        "required_roles": ["facility", "tier", "material_grade"],
                    }
                ],
                consequent={
                    "relation": "TIER_GRADE_MATCH",
                    "role_mapping": {"entity_a": "facility", "grade": "material_grade"},
                },
                min_confidence=0.5,
                certainty_factor=0.85,
                hop_decay=0.95,
                description="Facility processing tier aligns with material grade spec",
            ),
        ]

    def register_rule(self, rule: NAryRule) -> None:
        self._rules.append(rule)

    def load_kb_facts(self, facts: list[NAryFact]) -> int:
        """Load facts with deduplication. Returns count of new facts added."""
        added = 0
        for fact in facts:
            key = fact._participants_key()
            if key in self._dedup_keys:
                continue
            self._dedup_keys.add(key)
            self._kb_facts.append(fact)
            for entity_id in fact.participants.values():
                self._entity_index.setdefault(entity_id, []).append(fact)
            added += 1
        logger.info(
            "NAryInferenceEngine: loaded %d new facts (%d skipped duplicates)",
            added,
            len(facts) - added,
        )
        return added

    def infer(
        self,
        entity_id: str,
        relation_filter: str | None = None,
    ) -> list[NAryInferenceResult]:
        """Run n-ary inference for entity_id.

        Confidence model (certainty factors):
          CF_inferred = CF_rule * CF_evidence * hop_decay ^ hop_count
          Clamped to [0, 1].
        """
        relevant_facts = self._entity_index.get(entity_id, [])
        if not relevant_facts:
            return []

        results: list[NAryInferenceResult] = []

        for rule in self._rules:
            if relation_filter and rule.consequent.get("relation") != relation_filter:
                continue

            matched_facts = self._match_antecedents(relevant_facts, rule)
            if not matched_facts:
                continue

            for fact, hop_count in matched_facts:
                if fact.confidence < rule.min_confidence:
                    continue

                # Certainty factor model
                cf = safe_float(
                    np.clip(
                        rule.certainty_factor * fact.confidence * (rule.hop_decay**hop_count),
                        0.0,
                        1.0,
                    )
                )

                role_map = rule.consequent.get("role_mapping", {})
                consequent_participants = {
                    new_role: fact.participants[src_role]
                    for new_role, src_role in role_map.items()
                    if fact.participants.get(src_role)
                }
                if not consequent_participants:
                    continue

                # Qualifier trace: record which qualifiers contributed
                qualifier_trace = dict(fact.qualifiers) if fact.qualifiers else {}

                explanation = (
                    f"Rule '{rule.name}' on {fact.relation} "
                    f"(evidence_cf={fact.confidence:.2f} × "
                    f"rule_cf={rule.certainty_factor:.2f} × "
                    f"decay^{hop_count}={rule.hop_decay**hop_count:.3f}) "
                    f"→ {rule.consequent['relation']} (cf={cf:.3f})"
                )

                results.append(
                    NAryInferenceResult(
                        inferred_relation=rule.consequent["relation"],
                        participants=consequent_participants,
                        confidence=cf,
                        supporting_facts=[fact],
                        rule_name=rule.name,
                        hop_count=hop_count,
                        qualifier_trace=qualifier_trace,
                        explanation=explanation,
                    )
                )

        results.sort(key=lambda x: -x.confidence)
        return results

    def match_kb_facts(
        self,
        entity_id: str,
        query_qualifiers: dict[str, Any] | None = None,
        top_k: int = 10,
        min_confidence: float = 0.5,
    ) -> list[tuple[NAryFact, float]]:
        """Find KB facts most relevant to entity_id.

        Relevance = confidence * qualifier_alignment_score.
        query_qualifiers: optional context qualifiers from the enrichment
          pass (e.g., {"material_grade": "HDPE-2", "mfi_range": "4.0-6.0"}).
        """
        facts = self._entity_index.get(entity_id, [])
        query_quals = query_qualifiers or {}
        scored = []
        for f in facts:
            if f.confidence < min_confidence:
                continue
            qa_score = _qualifier_alignment_score(query_quals, f.qualifiers)
            relevance = safe_float(np.clip(f.confidence * qa_score, 0.0, 1.0))
            scored.append((f, relevance))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]

    def combined_infer(
        self,
        entity_id: str,
        rule_engine_results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge n-ary inferences with binary RuleEngine results.

        Produces a unified list in inference_bridge_v2 InferenceResult format,
        sorted by confidence descending. Both sources contribute to the
        same downstream scoring pass without separate aggregation.
        """
        nary_results = self.infer(entity_id)
        combined: list[dict[str, Any]] = list(rule_engine_results)

        for result in nary_results:
            combined.append(result.to_rule_engine_format())

        combined.sort(key=lambda x: -safe_float(x.get("confidence", 0.0)))
        return combined

    def _match_antecedents(
        self,
        facts: list[NAryFact],
        rule: NAryRule,
    ) -> list[tuple[NAryFact, int]]:
        """Return (fact, hop_count) pairs matching any antecedent pattern.

        hop_count = antecedent index (0-based) for decay calculation.
        Qualifier constraints checked if present in antecedent pattern.
        """
        matched = []
        for fact in facts:
            for hop, antecedent in enumerate(rule.antecedents):
                req_relation = antecedent.get("relation")
                req_roles = set(antecedent.get("required_roles", []))
                qual_constraints: dict[str, Any] = antecedent.get("qualifier_constraints", {})

                if fact.relation != req_relation:
                    continue
                if not req_roles.issubset(set(fact.participants.keys())):
                    continue
                # Check qualifier constraints if specified
                if qual_constraints:
                    qa = _qualifier_alignment_score(qual_constraints, fact.qualifiers)
                    if qa < 0.5:
                        continue
                matched.append((fact, hop))
                break
        return matched


import numpy as np  # noqa: E402 — placed here to avoid circular in type stubs
