"""
Lead Router Agent.

Confidence-scored lead routing based on enrichment data, ICP matching,
and territory rules. Routes leads to the best-fit sales representative.

L9 Architecture Note:
    Chassis-agnostic. Receives enrichment results, returns routing decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.utils.safe_convert import safe_float

logger = logging.getLogger(__name__)


@dataclass
class RoutingDecision:
    """Result of lead routing analysis."""

    assigned_rep: str
    confidence: float
    reasoning: list[str] = field(default_factory=list)
    fallback_reps: list[str] = field(default_factory=list)
    score_breakdown: dict[str, float] = field(default_factory=dict)


class LeadRouterAgent:
    """Routes leads based on enrichment quality and ICP match."""

    def __init__(self, team_config: dict[str, Any] | None = None) -> None:
        self.team_config = team_config or {}

    def route(
        self,
        lead_data: dict[str, Any],
        enrichment_scores: dict[str, float] | None = None,
    ) -> RoutingDecision:
        """
        Route a lead to the best sales rep.

        Scoring factors:
        1. Territory match (location-based)
        2. Industry specialization
        3. Deal size fit
        4. Current capacity
        """
        enrichment_scores = enrichment_scores or {}
        reps = self.team_config.get("reps", [])

        if not reps:
            return RoutingDecision(
                assigned_rep="unassigned",
                confidence=0.0,
                reasoning=["No sales reps configured"],
            )

        scored_reps: list[tuple[str, float, list[str]]] = []

        for rep in reps:
            rep_name = rep.get("name", "unknown")
            score = 0.0
            reasons: list[str] = []

            # Territory match
            territories = rep.get("territories", [])
            lead_country = lead_data.get("company_location_country", "")
            lead_state = lead_data.get("company_location_state", "")
            if lead_country in territories or lead_state in territories:
                score += 0.3
                reasons.append(f"Territory match: {lead_country}/{lead_state}")

            # Industry specialization
            industries = rep.get("industries", [])
            lead_industry = lead_data.get("company_industry", "")
            if lead_industry in industries:
                score += 0.25
                reasons.append(f"Industry match: {lead_industry}")

            # Deal size fit
            min_deal = rep.get("min_deal_size", 0)
            max_deal = rep.get("max_deal_size", safe_float("inf"))
            deal_size = lead_data.get("estimated_deal_size", 0)
            if min_deal <= deal_size <= max_deal:
                score += 0.2
                reasons.append("Deal size within range")

            # Enrichment quality bonus
            quality = enrichment_scores.get("overall", 0.0)
            if quality > 0.8:
                score += 0.15
                reasons.append(f"High enrichment quality: {quality:.2f}")

            # Capacity penalty
            current_load = rep.get("current_pipeline_count", 0)
            max_load = rep.get("max_pipeline", 50)
            if current_load < max_load * 0.8:
                score += 0.1
                reasons.append("Rep has capacity")
            else:
                score -= 0.1
                reasons.append("Rep near capacity")

            scored_reps.append((rep_name, score, reasons))

        scored_reps.sort(key=lambda x: x[1], reverse=True)
        best = scored_reps[0]

        return RoutingDecision(
            assigned_rep=best[0],
            confidence=min(best[1], 1.0),
            reasoning=best[2],
            fallback_reps=[r[0] for r in scored_reps[1:3]],
            score_breakdown={r[0]: r[1] for r in scored_reps},
        )
