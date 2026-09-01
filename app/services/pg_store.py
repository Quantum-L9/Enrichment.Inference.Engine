"""
app/services/pg_store.py

PostgreSQL async store — connection pool, CRUD, and query helpers.

Module-level engine (one per process). All writes are idempotent where possible.
Callers acquire a session via get_session() context manager.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import selectinload

from .pg_models import (
    Base,
    ConvergenceRun,
    EnrichmentResult,
    FieldConfidenceHistory,
    SchemaProposalRecord,
)

logger = structlog.get_logger("pg_store")

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

_TERMINAL_STATES = frozenset(
    {"converged", "budget_exhausted", "max_passes", "human_hold", "failed"}
)


def init_engine(
    database_url: str,
    pool_size: int = 10,
    max_overflow: int = 20,
) -> None:
    """Initialize the module-level async engine. Call once at startup."""
    global _engine, _session_factory
    _engine = create_async_engine(
        database_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        echo=False,
    )
    _session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    logger.info("pg_engine_initialized", pool_size=pool_size, max_overflow=max_overflow)


async def create_tables() -> None:
    """Create all tables. Use Alembic in production; this is for testing only."""
    if _engine is None:
        raise RuntimeError("Call init_engine() before create_tables()")
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_engine() -> None:
    """Dispose the connection pool on application shutdown."""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
    logger.info("pg_engine_closed")


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield an async session. Commits on success, rolls back on exception."""
    if _session_factory is None:
        raise RuntimeError("Call init_engine() before requesting a session")
    async with _session_factory() as session, session.begin():
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise


# ── EnrichmentResult CRUD ──────────────────────────────────────────────────


async def save_enrichment_result(
    tenant_id: str,
    entity_id: str,
    object_type: str,
    fields: dict[str, Any],
    confidence: float,
    uncertainty_score: float,
    tokens_used: int,
    processing_time_ms: int,
    pass_count: int,
    state: str = "completed",
    quality_tier: str = "unknown",
    inferences: list[dict[str, Any]] | None = None,
    kb_fragment_ids: list[str] | None = None,
    feature_vector: dict[str, Any] | None = None,
    domain: str | None = None,
    idempotency_key: str | None = None,
    failure_reason: str | None = None,
    convergence_run_id: uuid.UUID | None = None,
    field_confidence_map: dict[str, float] | None = None,
) -> EnrichmentResult:
    """
    Persist an EnrichResponse. Returns the saved ORM record.

    If idempotency_key is provided and a record already exists, returns
    the existing record without writing (idempotent replay safety).
    If field_confidence_map is provided, FieldConfidenceHistory rows are
    written in the same transaction.
    """
    if idempotency_key:
        existing = await get_enrichment_result_by_idempotency_key(
            idempotency_key, tenant_id=tenant_id
        )
        if existing:
            logger.debug(
                "enrichment_result_idempotent_hit",
                idempotency_key=idempotency_key,
                tenant_id=tenant_id,
            )
            return existing

    record = EnrichmentResult(
        tenant_id=tenant_id,
        entity_id=entity_id,
        object_type=object_type,
        domain=domain,
        idempotency_key=idempotency_key,
        fields=fields,
        confidence=confidence,
        uncertainty_score=uncertainty_score,
        quality_tier=quality_tier,
        state=state,
        failure_reason=failure_reason,
        tokens_used=tokens_used,
        processing_time_ms=processing_time_ms,
        pass_count=pass_count,
        inferences=inferences or [],
        kb_fragment_ids=kb_fragment_ids or [],
        feature_vector=feature_vector,
        convergence_run_id=convergence_run_id,
    )

    async with get_session() as session:
        session.add(record)
        await session.flush()

        if field_confidence_map:
            for field_name, conf in field_confidence_map.items():
                fch = FieldConfidenceHistory(
                    enrichment_result_id=record.id,
                    tenant_id=tenant_id,
                    entity_id=entity_id,
                    field_name=field_name,
                    field_value=str(fields.get(field_name, "")),
                    confidence=conf,
                    source="enrichment",
                    pass_number=pass_count,
                )
                session.add(fch)

    logger.info(
        "enrichment_result_saved",
        id=str(record.id),
        entity_id=entity_id,
        fields_count=len(fields),
        confidence=confidence,
    )
    return record


async def get_enrichment_result(result_id: uuid.UUID) -> EnrichmentResult | None:
    """Fetch a single enrichment result by primary key with field history."""
    async with get_session() as session:
        stmt = (
            select(EnrichmentResult)
            .options(selectinload(EnrichmentResult.field_confidence_history))
            .where(EnrichmentResult.id == result_id)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def get_enrichment_result_by_idempotency_key(
    idempotency_key: str,
    tenant_id: str,
) -> EnrichmentResult | None:
    """Look up an existing result by caller-supplied idempotency key, per tenant.

    `tenant_id` is required, not optional. An idempotency key is chosen by the
    caller and is only unique within that caller's own tenant: two tenants
    picking the same string is ordinary, not a collision to resolve. Matching on
    the key alone returned one tenant's stored enrichment to another — a
    cross-tenant read, and a silently dropped write for the second tenant, whose
    own operation was reported complete against a record it does not own.
    """
    async with get_session() as session:
        stmt = select(EnrichmentResult).where(
            EnrichmentResult.idempotency_key == idempotency_key,
            EnrichmentResult.tenant_id == tenant_id,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def get_latest_enrichment_for_entity(
    tenant_id: str, entity_id: str
) -> EnrichmentResult | None:
    """Return the most recently completed enrichment for an entity."""
    async with get_session() as session:
        stmt = (
            select(EnrichmentResult)
            .where(
                EnrichmentResult.tenant_id == tenant_id,
                EnrichmentResult.entity_id == entity_id,
                EnrichmentResult.state == "completed",
            )
            .order_by(EnrichmentResult.created_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


# ── ConvergenceRun CRUD ────────────────────────────────────────────────────


async def create_convergence_run(
    tenant_id: str,
    entity_id: str,
    domain: str,
    max_passes: int = 5,
    max_budget_tokens: int = 50000,
    domain_yaml_version_before: str | None = None,
) -> ConvergenceRun:
    """Create a new convergence run record at loop start."""
    run = ConvergenceRun(
        tenant_id=tenant_id,
        entity_id=entity_id,
        domain=domain,
        max_passes=max_passes,
        max_budget_tokens=max_budget_tokens,
        domain_yaml_version_before=domain_yaml_version_before,
        state="running",
    )
    async with get_session() as session:
        session.add(run)
        await session.flush()
    logger.info(
        "convergence_run_created",
        run_id=str(run.id),
        entity_id=entity_id,
        domain=domain,
    )
    return run


async def update_convergence_run(
    run_id: uuid.UUID,
    current_pass: int | None = None,
    state: str | None = None,
    convergence_reason: str | None = None,
    accumulated_fields: dict[str, Any] | None = None,
    accumulated_confidences: dict[str, float] | None = None,
    pass_results: list[dict[str, Any]] | None = None,
    total_tokens: int | None = None,
    total_cost_usd: float | None = None,
    schema_proposals: list[dict[str, Any]] | None = None,
    domain_yaml_version_after: str | None = None,
) -> None:
    """Partial update — only provided kwargs are written to the database."""
    values: dict[str, Any] = {"updated_at": datetime.now(UTC)}
    if current_pass is not None:
        values["current_pass"] = current_pass
    if state is not None:
        values["state"] = state
        if state in _TERMINAL_STATES:
            values["completed_at"] = datetime.now(UTC)
    if convergence_reason is not None:
        values["convergence_reason"] = convergence_reason
    if accumulated_fields is not None:
        values["accumulated_fields"] = accumulated_fields
    if accumulated_confidences is not None:
        values["accumulated_confidences"] = accumulated_confidences
    if pass_results is not None:
        values["pass_results"] = pass_results
    if total_tokens is not None:
        values["total_tokens"] = total_tokens
    if total_cost_usd is not None:
        values["total_cost_usd"] = total_cost_usd
    if schema_proposals is not None:
        values["schema_proposals"] = schema_proposals
    if domain_yaml_version_after is not None:
        values["domain_yaml_version_after"] = domain_yaml_version_after

    async with get_session() as session:
        await session.execute(
            update(ConvergenceRun).where(ConvergenceRun.id == run_id).values(**values)
        )


async def get_convergence_run(run_id: uuid.UUID) -> ConvergenceRun | None:
    """Load a convergence run by primary key."""
    async with get_session() as session:
        stmt = select(ConvergenceRun).where(ConvergenceRun.id == run_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def list_active_convergence_runs(
    tenant_id: str, domain: str | None = None
) -> list[ConvergenceRun]:
    """Return all in-progress convergence runs for a tenant."""
    async with get_session() as session:
        stmt = select(ConvergenceRun).where(
            ConvergenceRun.tenant_id == tenant_id,
            ConvergenceRun.state == "running",
        )
        if domain:
            stmt = stmt.where(ConvergenceRun.domain == domain)
        stmt = stmt.order_by(ConvergenceRun.created_at.desc())
        result = await session.execute(stmt)
        return list(result.scalars().all())


# ── SchemaProposal CRUD ────────────────────────────────────────────────────


async def save_schema_proposal(
    tenant_id: str,
    domain: str,
    batch_run_id: str,
    field_name: str,
    field_type: str,
    source: str,
    fill_rate: float,
    avg_confidence: float,
    sample_values: list[Any] | None = None,
    value_distribution: dict[str, Any] | None = None,
    proposed_gate: str | None = None,
    proposed_scoring_dimension: str | None = None,
    yaml_diff: str | None = None,
) -> SchemaProposalRecord:
    """Persist a schema proposal. Upserts on (tenant, domain, batch, field)."""
    async with get_session() as session:
        stmt = select(SchemaProposalRecord).where(
            SchemaProposalRecord.tenant_id == tenant_id,
            SchemaProposalRecord.domain == domain,
            SchemaProposalRecord.batch_run_id == batch_run_id,
            SchemaProposalRecord.field_name == field_name,
        )
        result = await session.execute(stmt)
        record = result.scalar_one_or_none()

        if record is None:
            record = SchemaProposalRecord(
                tenant_id=tenant_id,
                domain=domain,
                batch_run_id=batch_run_id,
                field_name=field_name,
            )
            session.add(record)

        record.field_type = field_type
        record.source = source
        record.fill_rate = fill_rate
        record.avg_confidence = avg_confidence
        record.sample_values = sample_values or []
        record.value_distribution = value_distribution or {}
        record.proposed_gate = proposed_gate
        record.proposed_scoring_dimension = proposed_scoring_dimension
        record.yaml_diff = yaml_diff
        await session.flush()
        return record


async def get_pending_schema_proposals(tenant_id: str, domain: str) -> list[SchemaProposalRecord]:
    """Return all pending proposals for a domain, ranked by confidence."""
    async with get_session() as session:
        stmt = (
            select(SchemaProposalRecord)
            .where(
                SchemaProposalRecord.tenant_id == tenant_id,
                SchemaProposalRecord.domain == domain,
                SchemaProposalRecord.approval_status == "pending",
            )
            .order_by(SchemaProposalRecord.avg_confidence.desc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def approve_schema_proposal(proposal_id: uuid.UUID, reviewed_by: str, approved: bool) -> None:
    """Record a human approval or rejection decision on a schema proposal."""
    async with get_session() as session:
        await session.execute(
            update(SchemaProposalRecord)
            .where(SchemaProposalRecord.id == proposal_id)
            .values(
                approval_status="approved" if approved else "rejected",
                reviewed_by=reviewed_by,
                reviewed_at=datetime.now(UTC),
            )
        )
