"""
app/services/pg_models.py

SQLAlchemy ORM models for the Enrichment Inference Engine persistence layer.

Tables:
  enrichment_results      — one row per completed enrichment
  convergence_runs        — one row per convergence loop run (multi-pass)
  field_confidence_history — time-series per-field confidence values
  schema_proposals        — schema proposals with human approval workflow

All tables use:
  - UUID primary keys (PostgreSQL native UUID type)
  - JSONB for variable-width dicts/lists
  - Composite indexes for tenant-scoped queries
  - server_default=func.now() for created_at/updated_at
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class EnrichmentResult(Base):
    """
    Persists the output of a single enrichment call.

    Written by ResultStore.persist_enrich_response() after each completed
    enrichment. The idempotency_key prevents duplicate writes on retry.
    """

    __tablename__ = "enrichment_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    object_type: Mapped[str] = mapped_column(String(128), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Not globally unique: an idempotency key is caller-chosen and is only
    # unique inside the tenant that chose it. See the tenant-scoped
    # UniqueConstraint in __table_args__.
    idempotency_key: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    uncertainty_score: Mapped[float] = mapped_column(Numeric(8, 4), nullable=False)
    quality_tier: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="completed", index=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processing_time_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pass_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    inferences: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    kb_fragment_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    feature_vector: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    convergence_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("convergence_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    convergence_run: Mapped[ConvergenceRun | None] = relationship(
        "ConvergenceRun", back_populates="final_result", foreign_keys=[convergence_run_id]
    )
    field_confidence_history: Mapped[list[FieldConfidenceHistory]] = relationship(
        "FieldConfidenceHistory",
        back_populates="enrichment_result",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_enrichment_results_tenant_entity", "tenant_id", "entity_id"),
        Index("ix_enrichment_results_tenant_created", "tenant_id", "created_at"),
        # Idempotency is per tenant. A UNIQUE on idempotency_key alone made one
        # tenant's key collide with another's: the second tenant's own operation
        # either resolved to the first tenant's stored row (a cross-tenant read)
        # or failed to insert. Two tenants picking the same string is ordinary.
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_enrichment_results_tenant_idempotency_key",
        ),
    )


class ConvergenceRun(Base):
    """
    Tracks a full multi-pass convergence loop execution.

    Written after every pass. A restarted process loads this record and
    resumes from current_pass + 1 — full crash recovery without re-running
    completed passes.
    """

    __tablename__ = "convergence_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String(128), nullable=False)
    domain_yaml_version_before: Mapped[str | None] = mapped_column(String(64), nullable=True)
    domain_yaml_version_after: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # No column-level index=True here: the explicit Index("ix_convergence_runs_state")
    # in __table_args__ is the one index on this column. Declaring both emitted two
    # CREATE INDEX statements with the same name, so create_all() failed on a
    # fresh database with DuplicateTableError.
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    convergence_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    current_pass: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_passes: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    accumulated_fields: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    accumulated_confidences: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    pass_results: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False, default=0.0)
    max_budget_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=50000)
    schema_proposals: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    final_result: Mapped[EnrichmentResult | None] = relationship(
        "EnrichmentResult",
        back_populates="convergence_run",
        foreign_keys=[EnrichmentResult.convergence_run_id],
        uselist=False,
    )

    __table_args__ = (
        Index("ix_convergence_runs_tenant_entity", "tenant_id", "entity_id"),
        Index("ix_convergence_runs_state", "state"),
    )


class FieldConfidenceHistory(Base):
    """Time-series per-field confidence values across enrichment passes."""

    __tablename__ = "field_confidence_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    enrichment_result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("enrichment_results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(256), nullable=False)
    field_name: Mapped[str] = mapped_column(String(256), nullable=False)
    field_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    pass_number: Mapped[int] = mapped_column(Integer, nullable=False)
    variation_agreement: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    enrichment_result: Mapped[EnrichmentResult] = relationship(
        "EnrichmentResult", back_populates="field_confidence_history"
    )

    __table_args__ = (
        Index("ix_field_confidence_entity_field", "tenant_id", "entity_id", "field_name"),
        Index("ix_field_confidence_result_pass", "enrichment_result_id", "pass_number"),
    )


class SchemaProposalRecord(Base):
    """Schema proposals from SchemaProposer with human approval workflow."""

    __tablename__ = "schema_proposals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    batch_run_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(256), nullable=False)
    field_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    fill_rate: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    avg_confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    sample_values: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    value_distribution: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    proposed_gate: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_scoring_dimension: Mapped[str | None] = mapped_column(Text, nullable=True)
    yaml_diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", index=True
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "domain",
            "batch_run_id",
            "field_name",
            name="uq_schema_proposal_batch_field",
        ),
        Index("ix_schema_proposals_domain_status", "domain", "approval_status"),
    )
