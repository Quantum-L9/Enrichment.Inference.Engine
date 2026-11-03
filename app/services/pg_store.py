"""
app/services/pg_store.py

PostgreSQL async store — connection pool, CRUD, and query helpers.

Module-level engine (one per process). All writes are idempotent where possible.
Callers acquire a session via get_session() context manager.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import structlog
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import selectinload

from app.engines.convergence.schema_proposer import _is_canonical_field_name
from app.engines.domain_yaml_reader import is_legal_tenant_id

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


_NODE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DISCOVERY_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PROPOSAL_IDENTITY = ("tenant_id", "domain", "batch_run_id", "node_label", "field_name")
_PROPOSAL_PAYLOAD = (
    *_PROPOSAL_IDENTITY,
    "discovery_spec_sha256",
    "ownership_contract_version",
    "field_type",
    "source",
    "fill_rate",
    "avg_confidence",
    "sample_values",
    "value_distribution",
    "proposed_gate",
    "proposed_scoring_dimension",
    "yaml_diff",
)


def validate_schema_proposal_binding(record: Any) -> tuple[str, str]:
    """Return recorded (node, discovery digest); never infer legacy ownership.

    Accept ORM/attribute records and mapping DTOs. All absent/NULL binding
    fields produce the stable ``ValueError('Schema proposal is legacy-unbound')``.
    Partial or malformed binding also fails closed. This checks provenance
    shape, not retained discovery bytes or membership in the ontology.
    """

    def value(name: str) -> Any:
        return record.get(name) if isinstance(record, Mapping) else getattr(record, name, None)

    node = value("node_label")
    digest = value("discovery_spec_sha256")
    version = value("ownership_contract_version")
    if node is None and digest is None and version is None:
        raise ValueError("Schema proposal is legacy-unbound")
    if type(version) is not int or version != 1:
        raise ValueError("Schema proposal ownership_contract_version must be 1")
    if not isinstance(node, str) or len(node) > 128 or not _NODE_IDENTIFIER.fullmatch(node):
        raise ValueError(
            "Schema proposal node_label must be an identifier of at most 128 characters"
        )
    if not isinstance(digest, str) or not _DISCOVERY_DIGEST.fullmatch(digest):
        raise ValueError(
            "Schema proposal discovery_spec_sha256 must be 64 lowercase hex characters"
        )
    return node, digest


def _proposal_text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > limit:
        raise ValueError(
            f"Schema proposal {name} must be nonblank, unpadded text of at most {limit} characters"
        )
    return value


def _validate_proposal_scope(tenant_id: str, domain: str, batch_run_id: str) -> None:
    if not is_legal_tenant_id(tenant_id) or tenant_id != domain:
        raise ValueError("Schema proposal tenant_id must equal a valid domain")
    _proposal_text(batch_run_id, "batch_run_id", 256)


def _proposal_rate(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"Schema proposal {name} must be a finite number between 0 and 1")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > 1:
            raise ValueError(f"Schema proposal {name} must be a finite number between 0 and 1")
        # Match Numeric(5, 4) before writing/comparing a durable retry.
        return number.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    except InvalidOperation as error:
        raise ValueError(f"Schema proposal {name} is not representable") from error


def _proposal_json(value: Any, name: str, expected_type: type) -> Any:
    if not isinstance(value, expected_type):
        raise ValueError(f"Schema proposal {name} has an invalid JSON container")
    try:
        # JSON round-trip validates finite JSON data and snapshots caller-owned
        # nested containers before the first await or transaction is opened.
        return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise ValueError(f"Schema proposal {name} must contain finite JSON data") from error


def _validated_proposal_values(
    tenant_id: str, domain: str, batch_run_id: str, proposal: Mapping[str, Any]
) -> dict[str, Any]:
    node, digest = validate_schema_proposal_binding(proposal)
    for name, expected in (
        ("tenant_id", tenant_id),
        ("domain", domain),
        ("batch_run_id", batch_run_id),
    ):
        if name in proposal and proposal[name] != expected:
            raise ValueError(f"Schema proposal {name} conflicts with batch scope")
    field_name = proposal.get("field_name")
    if (
        not isinstance(field_name, str)
        or len(field_name) > 256
        or not _is_canonical_field_name(field_name)
    ):
        raise ValueError("Schema proposal field_name must be canonical and at most 256 characters")
    values = {
        "tenant_id": tenant_id,
        "domain": domain,
        "batch_run_id": batch_run_id,
        "node_label": node,
        "discovery_spec_sha256": digest,
        "ownership_contract_version": 1,
        "field_name": field_name,
        "field_type": _proposal_text(proposal.get("field_type"), "field_type", 64),
        "source": _proposal_text(proposal.get("source"), "source", 32),
        "fill_rate": _proposal_rate(proposal.get("fill_rate"), "fill_rate"),
        "avg_confidence": _proposal_rate(proposal.get("avg_confidence"), "avg_confidence"),
        "sample_values": _proposal_json(
            proposal.get("sample_values") if proposal.get("sample_values") is not None else [],
            "sample_values",
            list,
        ),
        "value_distribution": _proposal_json(
            proposal.get("value_distribution")
            if proposal.get("value_distribution") is not None
            else {},
            "value_distribution",
            dict,
        ),
    }
    for name in ("proposed_gate", "proposed_scoring_dimension", "yaml_diff"):
        value = proposal.get(name)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"Schema proposal {name} must be text or None")
        values[name] = value
    return values


def _compare_proposal_payload(record: SchemaProposalRecord, values: dict[str, Any]) -> None:
    validate_schema_proposal_binding(record)
    for name in _PROPOSAL_PAYLOAD:
        actual = getattr(record, name)
        expected = values[name]
        if name in {"fill_rate", "avg_confidence"}:
            matches = _proposal_rate(actual, name) == expected
        elif name in {"sample_values", "value_distribution"}:
            matches = json.dumps(actual, sort_keys=True, allow_nan=False) == json.dumps(
                expected, sort_keys=True, allow_nan=False
            )
        else:
            matches = actual == expected
        if not matches:
            raise ValueError(f"Conflicting schema proposal retry: immutable {name} differs")


def _review_proposal(
    record: SchemaProposalRecord, reviewed_by: str, approved: bool, reviewed_at: datetime
) -> None:
    status = "approved" if approved else "rejected"
    if (
        record.approval_status == "pending"
        and record.reviewed_by is None
        and record.reviewed_at is None
    ):
        record.approval_status = status
        record.reviewed_by = reviewed_by
        record.reviewed_at = reviewed_at
    elif (
        record.approval_status != status
        or record.reviewed_by != reviewed_by
        or record.reviewed_at is None
    ):
        raise ValueError("Conflicting schema proposal retry: decision or reviewer differs")


async def _insert_bound_proposal(
    session: AsyncSession, values: dict[str, Any]
) -> SchemaProposalRecord:
    # Unique index arbitrates concurrent writers. DO NOTHING cannot overwrite
    # the winning payload; the following locked reload must compare it.
    insert_statement = (
        insert(SchemaProposalRecord)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=[getattr(SchemaProposalRecord, name) for name in _PROPOSAL_IDENTITY],
            index_where=text("ownership_contract_version = 1"),
        )
    )
    await session.execute(insert_statement)
    reload_statement = (
        select(SchemaProposalRecord)
        .where(
            *(getattr(SchemaProposalRecord, name) == values[name] for name in _PROPOSAL_IDENTITY),
            SchemaProposalRecord.ownership_contract_version == 1,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await session.execute(reload_statement)
    record = result.scalar_one_or_none()
    if record is None:
        raise RuntimeError("Schema proposal conflict reload did not find the bound record")
    _compare_proposal_payload(record, values)
    return record


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
    *,
    node_label: str | None = None,
    discovery_spec_sha256: str | None = None,
    ownership_contract_version: int | None = None,
) -> SchemaProposalRecord:
    """Save immutable bound pending evidence; missing binding is refused.

    Keyword-only binding defaults support explicit legacy refusal, NOT writes.
    Identical pending retries return original records; reviewed records cannot
    be reset by a pending save. Only explicit approval methods may review them.
    """
    _validate_proposal_scope(tenant_id, domain, batch_run_id)
    values = _validated_proposal_values(
        tenant_id,
        domain,
        batch_run_id,
        {
            "node_label": node_label,
            "discovery_spec_sha256": discovery_spec_sha256,
            "ownership_contract_version": ownership_contract_version,
            "field_name": field_name,
            "field_type": field_type,
            "source": source,
            "fill_rate": fill_rate,
            "avg_confidence": avg_confidence,
            "sample_values": sample_values,
            "value_distribution": value_distribution,
            "proposed_gate": proposed_gate,
            "proposed_scoring_dimension": proposed_scoring_dimension,
            "yaml_diff": yaml_diff,
        },
    )
    values.update(approval_status="pending", reviewed_by=None, reviewed_at=None)
    async with get_session() as session:
        record = await _insert_bound_proposal(session, values)
        if (
            record.approval_status != "pending"
            or record.reviewed_by is not None
            or record.reviewed_at is not None
        ):
            raise ValueError(
                "Conflicting schema proposal retry: pending save cannot change a reviewed decision"
            )
        await session.flush()
        return record


async def persist_schema_proposal_batch(
    *,
    tenant_id: str,
    domain: str,
    batch_run_id: str,
    reviewed_by: str,
    decisions: list[dict[str, Any]],
) -> list[SchemaProposalRecord]:
    """Validate the entire owner-bound decision batch, then review atomically.

    Decisions carry all proposal columns, node_label, discovery_spec_sha256,
    ownership_contract_version=1, and a strict bool approved. The durable key
    is (tenant, domain, batch, node, field). Pending immutable evidence can be
    reviewed here. Exact retries retain their original review/creation times;
    conflicting payload, digest, decision or reviewer refuses the transaction.
    """
    _validate_proposal_scope(tenant_id, domain, batch_run_id)
    _proposal_text(reviewed_by, "reviewed_by", 256)
    if not isinstance(decisions, list):
        raise ValueError("Schema proposal decisions must be a list")
    validated: list[tuple[dict[str, Any], bool]] = []
    seen: set[tuple[str, str]] = set()
    for decision in decisions:
        if not isinstance(decision, Mapping):
            raise ValueError("Schema proposal decision must be a mapping")
        values = _validated_proposal_values(tenant_id, domain, batch_run_id, decision)
        approved = decision.get("approved")
        if type(approved) is not bool:
            raise ValueError("Schema proposal approved must be a bool")
        if "reviewed_by" in decision and decision["reviewed_by"] != reviewed_by:
            raise ValueError("Schema proposal reviewed_by conflicts with batch reviewer")
        key = (values["node_label"], values["field_name"])
        if key in seen:
            raise ValueError("Duplicate schema proposal identity in batch")
        seen.add(key)
        validated.append((values, approved))
    if not validated:
        return []
    reviewed_at = datetime.now(UTC)
    records: dict[tuple[str, str], SchemaProposalRecord] = {}
    async with get_session() as session:
        # Deterministic lock order avoids opposite-order mixed-owner retries
        # deadlocking, while the return list retains caller decision order.
        for values, approved in sorted(
            validated, key=lambda item: (item[0]["node_label"], item[0]["field_name"])
        ):
            incoming = {
                **values,
                "approval_status": "approved" if approved else "rejected",
                "reviewed_by": reviewed_by,
                "reviewed_at": reviewed_at,
            }
            record = await _insert_bound_proposal(session, incoming)
            _review_proposal(record, reviewed_by, approved, reviewed_at)
            records[(values["node_label"], values["field_name"])] = record
        await session.flush()
    ordered = [records[(values["node_label"], values["field_name"])] for values, _ in validated]
    logger.info(
        "schema_proposal_batch_persisted",
        tenant_id=tenant_id,
        domain=domain,
        batch_run_id=batch_run_id,
        rows=len(ordered),
        approved=sum(1 for r in ordered if r.approval_status == "approved"),
    )
    return ordered


async def get_pending_schema_proposals(tenant_id: str, domain: str) -> list[SchemaProposalRecord]:
    """Return pending records including legacy NULL binding, ranked by confidence."""
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


async def get_approved_schema_proposals(tenant_id: str, domain: str) -> list[SchemaProposalRecord]:
    """Return every approved proposal for a domain, oldest batch first.

    Includes unbound historical rows: consumers must validate every selected
    binding and refuse incomplete history rather than silently filter it.
    This audit ledger is not effective runtime YAML authority. Both tenant
    and domain remain query filters; caller authentication is unchanged.
    """
    async with get_session() as session:
        stmt = (
            select(SchemaProposalRecord)
            .where(
                SchemaProposalRecord.tenant_id == tenant_id,
                SchemaProposalRecord.domain == domain,
                SchemaProposalRecord.approval_status == "approved",
            )
            .order_by(
                SchemaProposalRecord.created_at.asc(),
                SchemaProposalRecord.node_label.asc(),
                SchemaProposalRecord.field_name.asc(),
            )
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def approve_schema_proposal(
    proposal_id: uuid.UUID, reviewed_by: str, approved: bool
) -> SchemaProposalRecord:
    """Review bound pending evidence by UUID, or return an identical reviewed retry.

    Missing and legacy-unbound records fail before update. No ownership is
    supplied here, no reviewer/timestamp is replaced on retry, and existing
    route authentication/authorization remains the caller's responsibility.
    """
    if not isinstance(proposal_id, uuid.UUID):
        raise ValueError("Schema proposal proposal_id must be a UUID")
    _proposal_text(reviewed_by, "reviewed_by", 256)
    if type(approved) is not bool:
        raise ValueError("Schema proposal approved must be a bool")
    async with get_session() as session:
        result = await session.execute(
            select(SchemaProposalRecord)
            .where(SchemaProposalRecord.id == proposal_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise ValueError("Schema proposal not found")
        validate_schema_proposal_binding(record)
        _validate_proposal_scope(record.tenant_id, record.domain, record.batch_run_id)
        _review_proposal(record, reviewed_by, approved, datetime.now(UTC))
        await session.flush()
        return record
