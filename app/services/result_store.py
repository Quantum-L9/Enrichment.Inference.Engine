"""
app/services/result_store.py

ResultStore — domain facade over pg_store.

Provides a clean API for convergence_controller and API layer without
coupling callers to SQLAlchemy internals. Handles serialization
and field confidence extraction automatically.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from app.utils.safe_convert import safe_float

from ..models.schemas import EnrichResponse
from . import pg_store
from .pg_models import ConvergenceRun, EnrichmentResult

logger = structlog.get_logger("result_store")


class ResultStore:
    """
    High-level persistence facade for enrichment results and convergence runs.

    Usage:
        store = ResultStore(tenant_id="acme")
        result_id = await store.persist_enrich_response(
            response, entity_id="cmp_001", object_type="Account"
        )
    """

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id

    async def persist_enrich_response(
        self,
        response: EnrichResponse,
        entity_id: str,
        object_type: str,
        domain: str | None = None,
        idempotency_key: str | None = None,
        convergence_run_id: uuid.UUID | None = None,
        field_confidence_map: dict[str, float] | None = None,
    ) -> uuid.UUID:
        """
        Persist a completed EnrichResponse.

        If field_confidence_map is not provided, it is extracted from
        response.feature_vector["confidence_tracking"] automatically.
        Returns the database record UUID.
        """
        if field_confidence_map is None and response.feature_vector:
            tracking = response.feature_vector.get("confidence_tracking", {})
            field_confidence_map = {
                fname: fdata.get("latest_confidence", response.confidence)
                for fname, fdata in tracking.items()
                if isinstance(fdata, dict)
            }

        record = await pg_store.save_enrichment_result(
            tenant_id=self.tenant_id,
            entity_id=entity_id,
            object_type=object_type,
            fields=response.fields,
            confidence=response.confidence,
            uncertainty_score=response.uncertainty_score,
            tokens_used=response.tokens_used,
            processing_time_ms=response.processing_time_ms,
            pass_count=response.pass_count,
            state=response.state,
            quality_tier=response.quality_tier,
            inferences=response.inferences,
            kb_fragment_ids=response.kb_fragment_ids,
            feature_vector=response.feature_vector,
            domain=domain,
            idempotency_key=idempotency_key,
            failure_reason=response.failure_reason,
            convergence_run_id=convergence_run_id,
            field_confidence_map=field_confidence_map,
        )
        result_id = record.id
        if not isinstance(result_id, uuid.UUID):
            raise TypeError("enrichment result id must be a UUID")
        return result_id

    async def get_result(self, result_id: uuid.UUID) -> EnrichmentResult | None:
        return await pg_store.get_enrichment_result(result_id, self.tenant_id)

    async def get_latest_for_entity(self, entity_id: str) -> EnrichmentResult | None:
        return await pg_store.get_latest_enrichment_for_entity(self.tenant_id, entity_id)

    async def start_convergence_run(
        self,
        entity_id: str,
        domain: str,
        max_passes: int = 5,
        max_budget_tokens: int = 50000,
        domain_yaml_version: str | None = None,
    ) -> uuid.UUID:
        """Create a convergence run record and return its ID."""
        run = await pg_store.create_convergence_run(
            tenant_id=self.tenant_id,
            entity_id=entity_id,
            domain=domain,
            max_passes=max_passes,
            max_budget_tokens=max_budget_tokens,
            domain_yaml_version_before=domain_yaml_version,
        )
        run_id = run.id
        if not isinstance(run_id, uuid.UUID):
            raise TypeError("convergence run id must be a UUID")
        return run_id

    async def checkpoint_convergence_pass(
        self,
        run_id: uuid.UUID,
        pass_number: int,
        accumulated_fields: dict[str, Any],
        accumulated_confidences: dict[str, float],
        pass_results: list[dict],
        total_tokens: int,
        total_cost_usd: float,
    ) -> None:
        """
        Persist state after each pass for crash recovery.

        A restarted process loads this checkpoint and resumes from
        pass_number + 1 without re-running completed passes.
        """
        await pg_store.update_convergence_run(
            run_id=run_id,
            tenant_id=self.tenant_id,
            current_pass=pass_number,
            accumulated_fields=accumulated_fields,
            accumulated_confidences=accumulated_confidences,
            pass_results=pass_results,
            total_tokens=total_tokens,
            total_cost_usd=total_cost_usd,
        )

    async def complete_convergence_run(
        self,
        run_id: uuid.UUID,
        state: str,
        convergence_reason: str,
        accumulated_fields: dict[str, Any],
        accumulated_confidences: dict[str, float],
        pass_results: list[dict],
        total_tokens: int,
        total_cost_usd: float,
        schema_proposals: list[dict] | None = None,
        domain_yaml_version_after: str | None = None,
    ) -> None:
        """Mark a convergence run as complete with final state."""
        await pg_store.update_convergence_run(
            run_id=run_id,
            tenant_id=self.tenant_id,
            state=state,
            convergence_reason=convergence_reason,
            accumulated_fields=accumulated_fields,
            accumulated_confidences=accumulated_confidences,
            pass_results=pass_results,
            total_tokens=total_tokens,
            total_cost_usd=total_cost_usd,
            schema_proposals=schema_proposals or [],
            domain_yaml_version_after=domain_yaml_version_after,
        )
        logger.info(
            "convergence_run_completed",
            run_id=str(run_id),
            state=state,
            reason=convergence_reason,
            total_tokens=total_tokens,
        )

    async def get_convergence_run(self, run_id: uuid.UUID) -> ConvergenceRun | None:
        return await pg_store.get_convergence_run(run_id, self.tenant_id)

    async def approve_schema_proposal(
        self, proposal_id: uuid.UUID, reviewed_by: str, approved: bool
    ) -> int:
        """Approve only this tenant's proposal. Cross-tenant UUID returns 0."""
        return await pg_store.approve_schema_proposal(
            self.tenant_id, proposal_id, reviewed_by, approved
        )

    async def list_active_runs(self, domain: str | None = None) -> list[ConvergenceRun]:
        return await pg_store.list_active_convergence_runs(self.tenant_id, domain)

    async def get_field_confidence_history(
        self, entity_id: str, field_name: str
    ) -> list[dict[str, Any]]:
        """
        Return per-field confidence history across all enrichment passes.

        Returns list of {pass_number, confidence, source, field_value,
        variation_agreement, recorded_at} sorted by pass_number ASC.
        Enables convergence analytics and diminishing-returns detection.
        """
        from sqlalchemy import select

        async with pg_store.get_session() as session:
            from .pg_models import FieldConfidenceHistory

            stmt = (
                select(FieldConfidenceHistory)
                .where(
                    FieldConfidenceHistory.tenant_id == self.tenant_id,
                    FieldConfidenceHistory.entity_id == entity_id,
                    FieldConfidenceHistory.field_name == field_name,
                )
                .order_by(
                    FieldConfidenceHistory.pass_number.asc(),
                    FieldConfidenceHistory.recorded_at.asc(),
                )
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [
                {
                    "pass_number": r.pass_number,
                    "confidence": safe_float(r.confidence),
                    "source": r.source,
                    "field_value": r.field_value,
                    "variation_agreement": (
                        safe_float(r.variation_agreement) if r.variation_agreement else None
                    ),
                    "recorded_at": r.recorded_at.isoformat(),
                }
                for r in rows
            ]
