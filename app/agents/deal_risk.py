"""
Deal Risk Assessment Agent.

Scores pipeline health and identifies at-risk deals based on
enrichment completeness, engagement signals, and historical patterns.

L9 Architecture Note:
    Chassis-agnostic. Receives deal data, returns risk assessment.

# L9-fix: NAME-FLAT (FALSE_POSITIVE)
# L9-file: app/agents/deal_risk.py
# L9-violation: NAME-FLAT fired on `recommendations: list[str]`
# L9-fix-summary: `recommendations` is a standard English word in valid snake_case.
#   Added # l9:flatcase-ok annotation per audit FP suppression protocol.
# L9-layer: engine/agent
# L9-node: enrichment-inference-engine
# L9-contract-version: 1.0.0
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RiskAssessment:
    """Result of deal risk analysis."""

    risk_score: float  # 0.0 = no risk, 1.0 = critical risk
    risk_level: str  # low, medium, high, critical
    risk_factors: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)  # l9:flatcase-ok -- single English word
    dimension_scores: dict[str, float] = field(default_factory=dict)


class DealRiskAgent:
    """Assesses deal risk based on enrichment and engagement data."""

    def assess(
        self,
        deal_data: dict[str, Any],
        enrichment_quality: float = 0.0,
        historical_context: dict[str, Any] | None = None,
    ) -> RiskAssessment:
        """
        Assess risk for a deal/opportunity.

        Risk dimensions:
        1. Data completeness — missing critical fields
        2. Engagement decay — declining activity signals
        3. Timeline risk — close date vs stage progression
        4. Stakeholder coverage — decision maker access
        5. Competitive pressure — known competitors
        """
        historical_context = historical_context or {}
        risk_factors: list[str] = []
        recommendations: list[str] = []  # l9:flatcase-ok -- single English word
        dimension_scores: dict[str, float] = {}

        # 1. Data completeness risk
        required_fields = [
            "opportunity_name",
            "opportunity_stage",
            "opportunity_amount",
            "opportunity_close_date",
            "opportunity_account_id",
        ]
        missing = [f for f in required_fields if not deal_data.get(f)]
        completeness_risk = len(missing) / len(required_fields)
        dimension_scores["data_completeness"] = completeness_risk
        if missing:
            risk_factors.append(f"Missing fields: {', '.join(missing)}")
            recommendations.append("Run enrichment to fill missing deal fields")

        # 2. Enrichment quality risk
        enrichment_risk = max(0.0, 1.0 - enrichment_quality)
        dimension_scores["enrichment_quality"] = enrichment_risk
        if enrichment_quality < 0.5:
            risk_factors.append(f"Low enrichment quality: {enrichment_quality:.2f}")
            recommendations.append("Trigger waterfall enrichment for account data")

        # 3. Timeline risk
        stage = deal_data.get("opportunity_stage", "")
        probability = deal_data.get("opportunity_probability", 0)
        if isinstance(probability, str):
            try:
                probability = float(probability)
            except (ValueError, TypeError):
                probability = 0
        timeline_risk = 0.0
        if stage in ("discovery", "qualification") and probability > 50:
            timeline_risk = 0.6
            risk_factors.append("High probability but early stage")
            recommendations.append("Validate stage progression criteria")
        elif stage in ("negotiation", "closed_won") and probability < 30:
            timeline_risk = 0.7
            risk_factors.append("Late stage but low probability")
            recommendations.append("Review deal blockers with rep")
        dimension_scores["timeline"] = timeline_risk

        # 4. Stakeholder coverage risk
        stakeholders = deal_data.get("opportunity_stakeholders", [])
        has_decision_maker = any(
            s.get("role", "").lower() in ("decision_maker", "economic_buyer", "champion")
            for s in stakeholders
            if isinstance(s, dict)
        )
        stakeholder_risk = 0.0 if has_decision_maker else 0.5
        dimension_scores["stakeholder_coverage"] = stakeholder_risk
        if not has_decision_maker:
            risk_factors.append("No identified decision maker")
            recommendations.append("Map buying committee and identify champion")

        # 5. Competitive pressure
        competitors = deal_data.get("opportunity_competitors", [])
        competitive_risk = min(len(competitors) * 0.2, 0.8) if competitors else 0.0
        dimension_scores["competitive_pressure"] = competitive_risk
        if len(competitors) > 2:
            risk_factors.append(f"{len(competitors)} known competitors")
            recommendations.append("Develop competitive differentiation strategy")

        # Weighted aggregate
        weights = {
            "data_completeness": 0.20,
            "enrichment_quality": 0.25,
            "timeline": 0.20,
            "stakeholder_coverage": 0.20,
            "competitive_pressure": 0.15,
        }
        risk_score = sum(dimension_scores.get(k, 0.0) * w for k, w in weights.items())
        risk_score = min(risk_score, 1.0)

        if risk_score < 0.25:
            risk_level = "low"
        elif risk_score < 0.50:
            risk_level = "medium"
        elif risk_score < 0.75:
            risk_level = "high"
        else:
            risk_level = "critical"

        return RiskAssessment(
            risk_score=risk_score,
            risk_level=risk_level,
            risk_factors=risk_factors,
            recommendations=recommendations,
            dimension_scores=dimension_scores,
        )
