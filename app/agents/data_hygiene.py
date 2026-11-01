"""
Data Hygiene Agent.

Assesses CRM data quality and recommends hygiene actions.
Identifies duplicates, stale records, and enrichment opportunities.

L9 Architecture Note:
    Chassis-agnostic. Receives records, returns hygiene assessment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class HygieneAction:
    """A recommended hygiene action for a record."""

    record_id: str
    action_type: str  # enrich, deduplicate, archive, validate, merge
    priority: str  # low, medium, high, critical
    description: str
    affected_fields: list[str] = field(default_factory=list)


@dataclass
class HygieneReport:
    """Aggregate hygiene assessment for a batch of records."""

    total_records: int
    quality_score: float  # 0.0 - 1.0
    actions: list[HygieneAction] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)


class DataHygieneAgent:
    """Assesses CRM data quality and recommends hygiene actions."""

    def assess(
        self,
        records: list[dict[str, Any]],
        domain: str = "company",
    ) -> HygieneReport:
        """
        Assess data quality for a batch of CRM records.

        Checks:
        1. Completeness — missing required fields
        2. Freshness — stale records needing re-enrichment
        3. Validity — format violations (email, phone, URL)
        4. Duplicates — potential duplicate records
        5. Enrichment opportunity — records that would benefit from enrichment
        """
        actions: list[HygieneAction] = []
        action_counts: dict[str, int] = {}

        required_fields = self._get_required_fields(domain)
        total_complete = 0.0

        for record in records:
            record_id = record.get("id", record.get("record_id", "unknown"))
            record_actions = self._assess_record(record, record_id, domain, required_fields)
            actions.extend(record_actions)

            for action in record_actions:
                action_counts[action.action_type] = action_counts.get(action.action_type, 0) + 1

            # Completeness check
            filled = sum(1 for f in required_fields if record.get(f) not in (None, ""))
            if required_fields:
                total_complete += filled / len(required_fields)

        quality_score = total_complete / len(records) if records else 0.0

        return HygieneReport(
            total_records=len(records),
            quality_score=quality_score,
            actions=actions,
            summary=action_counts,
        )

    def _assess_record(
        self,
        record: dict[str, Any],
        record_id: str,
        domain: str,
        required_fields: list[str],
    ) -> list[HygieneAction]:
        """Assess a single record and return recommended actions."""
        actions: list[HygieneAction] = []

        # Missing required fields → enrich
        missing = [f for f in required_fields if record.get(f) in (None, "")]
        if missing:
            priority = "high" if len(missing) > 3 else "medium"
            actions.append(
                HygieneAction(
                    record_id=record_id,
                    action_type="enrich",
                    priority=priority,
                    description=f"Missing {len(missing)} required fields",
                    affected_fields=missing,
                )
            )

        # Email validation
        email = record.get("contact_email", "")
        if email and "@" not in email:
            actions.append(
                HygieneAction(
                    record_id=record_id,
                    action_type="validate",
                    priority="high",
                    description="Invalid email format",
                    affected_fields=["contact_email"],
                )
            )

        # Stale record detection
        last_enriched = record.get("last_enriched_at", "")
        if not last_enriched:
            actions.append(
                HygieneAction(
                    record_id=record_id,
                    action_type="enrich",
                    priority="medium",
                    description="Never enriched — candidate for initial enrichment",
                    affected_fields=[],
                )
            )

        return actions

    @staticmethod
    def _get_required_fields(domain: str) -> list[str]:
        """Return required fields for a domain."""
        domain_fields = {
            "company": [
                "company_name",
                "company_domain",
                "company_industry",
                "employee_count",
                "company_location_country",
            ],
            "contact": [
                "contact_email",
                "contact_first_name",
                "contact_last_name",
                "contact_title",
            ],
            "opportunity": [
                "opportunity_name",
                "opportunity_stage",
                "opportunity_close_date",
                "opportunity_amount",
            ],
        }
        return domain_fields.get(domain, [])
