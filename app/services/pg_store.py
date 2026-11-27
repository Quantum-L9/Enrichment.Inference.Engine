"""
app/services/pg_store.py

PostgreSQL async store — connection pool, CRUD, and query helpers.

Module-level engine (one per process). All writes are idempotent where possible.
Callers acquire a session via get_session() context manager.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import structlog
from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    exists,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Mapped, aliased, mapped_column, selectinload

from app.engines.convergence.schema_proposer import _is_canonical_field_name
from app.engines.domain_yaml_reader import is_legal_tenant_id

from .graph_return_channel import GraphReturnCapacityError
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
    graph_intent = _graph_intent_var.get()
    if graph_intent is not None and graph_intent.tenant_id != tenant_id:
        # An intent is bound to the tenant whose result it follows; one bound
        # to another tenant would queue a graph write for a record it does not own.
        msg = f"graph intent tenant {graph_intent.tenant_id!r} != result tenant {tenant_id!r}"
        raise ValueError(msg)
    if idempotency_key:
        existing = await _idempotent_hit(tenant_id, idempotency_key, fields, graph_intent)
        if existing is not None:
            return existing

    record = EnrichmentResult(
        id=uuid.uuid4(),
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
        if graph_intent is not None:
            # Same transaction as the result (A2): a committed result always has
            # its graph obligation, and a second commit of the same operation
            # (another worker, a restart) rolls back whole on the intent's key.
            # The intent goes FIRST: a concurrent commit of the operation waits
            # on that key and leaves as a duplicate, before it can trip the
            # result's own idempotency index.
            await _insert_graph_intent(session, graph_intent, record.id)
        try:
            async with session.begin_nested():
                session.add(record)
                await session.flush()
        except IntegrityError as exc:
            # The idempotency pre-check above is not atomic: a concurrent save
            # with the same key can commit between it and this insert. That is
            # the replay the pre-check exists for, so it answers the same way.
            if not idempotency_key or "uq_enrichment_results_tenant_idempotency_key" not in str(
                exc.orig
            ):
                raise
            return await _idempotent_race_winner(
                session, tenant_id, idempotency_key, fields, graph_intent
            )

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


class SchemaProposalNotFoundError(LookupError, ValueError):
    """Missing proposal UUID.

    LookupError keeps the HTTP route on 404. ValueError keeps the documented
    service contract for non-HTTP callers.
    """


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
            raise SchemaProposalNotFoundError("Schema proposal not found")
        validate_schema_proposal_binding(record)
        _validate_proposal_scope(record.tenant_id, record.domain, record.batch_run_id)
        _review_proposal(record, reviewed_by, approved, datetime.now(UTC))
        await session.flush()
        return record


# ── Graph-sync intents (A2: durable graph obligation) ──────────────────────
#
# A committed enrichment result whose graph leg is asked for carries one
# `graph_sync_intents` row, written in the result's own transaction. The row
# pins exactly what the graph leg sends -- tenant, entity, node label, the
# admitted typed fields, the endpoint and its row-contract digest -- so a
# replay after an outage or a restart sends the same input, never whatever a
# later call carries. It is internal state: nothing here reaches a payload.
#
# States: `pending` (due at `next_attempt_at`), `dispatching` (claimed under a
# lease), and the terminal `acknowledged` (CEG confirmed it), `failed_terminal`
# (refused, or retries exhausted -- never reported as synced) and `superseded`
# (a newer version of the same subject was already sent). Versions of one
# subject (tenant, entity, node label) are sent in `seq` order: a newer one is
# never claimed while an older one is outstanding, so an old retry cannot land
# after -- and overwrite -- a newer graph state.

GRAPH_INTENT_PENDING = "pending"
GRAPH_INTENT_DISPATCHING = "dispatching"
GRAPH_INTENT_ACKNOWLEDGED = "acknowledged"
GRAPH_INTENT_FAILED_TERMINAL = "failed_terminal"
GRAPH_INTENT_SUPERSEDED = "superseded"
GRAPH_INTENT_TERMINAL_STATES = frozenset(
    {GRAPH_INTENT_ACKNOWLEDGED, GRAPH_INTENT_FAILED_TERMINAL, GRAPH_INTENT_SUPERSEDED}
)
GRAPH_INTENT_DEFAULT_MAX_ATTEMPTS = 8
# Max-apply window: how long after its last attempt a request whose outcome EIE
# never learned may still be applied by CEG. It bounds the Gate dispatch plus
# the worker's handling of one request, so it must be at least that bound
# (graph_sync_intents.md *Ordering*). A newer version of the subject is not
# sent until the window of every uncertain older version has passed.
GRAPH_MAX_APPLY_SECONDS = 300.0
# Recorded in `last_error` when the transport retried inside one claim: the
# claim's earlier request may still be applied, so the version is uncertain.
GRAPH_RETRIED_IN_CLAIM = "retried_in_claim"


class GraphSyncIntent(Base):
    """One durable graph-sync obligation of one committed enrichment result."""

    __tablename__ = "graph_sync_intents"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    seq: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), nullable=False, unique=True
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    effect_key: Mapped[str] = mapped_column(String(512), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(256), nullable=False)
    node_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(128), nullable=True)
    endpoint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    contract_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cartridge_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="enrich")
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    enrichment_result_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, default=GRAPH_INTENT_PENDING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=GRAPH_INTENT_DEFAULT_MAX_ATTEMPTS
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_ack: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "effect_key", name="uq_graph_sync_intents_tenant_key"),
        Index("ix_graph_sync_intents_due", "state", "next_attempt_at"),
        Index("ix_graph_sync_intents_subject", "tenant_id", "entity_id", "endpoint", "seq"),
    )


@dataclass(frozen=True, slots=True)
class GraphIntentSpec:
    """What a committed result's graph leg must send, pinned at commit."""

    tenant_id: str
    effect_key: str
    entity_id: str
    fields: Mapping[str, Any]
    domain: str | None = None
    node_label: str | None = None
    endpoint: str | None = None
    contract_digest: str | None = None
    cartridge_revision: str | None = None
    origin: str = "enrich"
    max_attempts: int = GRAPH_INTENT_DEFAULT_MAX_ATTEMPTS


@dataclass(frozen=True, slots=True)
class GraphIntentRecord:
    """A snapshot of one `graph_sync_intents` row."""

    id: uuid.UUID
    seq: int
    tenant_id: str
    effect_key: str
    entity_id: str
    node_label: str | None
    domain: str | None
    endpoint: str | None
    contract_digest: str | None
    cartridge_revision: str | None
    origin: str
    fields: dict[str, Any]
    enrichment_result_id: uuid.UUID | None
    state: str
    attempts: int
    max_attempts: int
    next_attempt_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error: str | None
    last_ack: dict[str, Any] | None

    @property
    def terminal(self) -> bool:
        return self.state in GRAPH_INTENT_TERMINAL_STATES


class GraphIntentDuplicateError(RuntimeError):
    """The operation's graph intent already exists: this commit is a duplicate.

    Raised inside the result's transaction, so the duplicate result row is
    rolled back with it. The earlier commit of the same operation owns the
    persisted record, its completion event and its graph obligation.
    """

    def __init__(self, tenant_id: str, effect_key: str) -> None:
        super().__init__(f"graph intent already recorded for {tenant_id}:{effect_key}")
        self.tenant_id = tenant_id
        self.effect_key = effect_key


class GraphIntentReplayMismatchError(RuntimeError):
    """A replay of a stored operation carries other entity or fields.

    Not a duplicate: the stored result does not hold what this call computed,
    so this call's result is not durable. The caller treats it as a failed
    persistence (refused when durability is required), never as `completed`.
    """

    def __init__(self, tenant_id: str, effect_key: str) -> None:
        super().__init__(f"replay of {tenant_id}:{effect_key} does not match the stored result")
        self.tenant_id = tenant_id
        self.effect_key = effect_key


_graph_intent_var: ContextVar[GraphIntentSpec | None] = ContextVar("graph_intent", default=None)


@contextmanager
def graph_intent_scope(spec: GraphIntentSpec | None) -> Iterator[None]:
    """Make `save_enrichment_result` record `spec` in the result's transaction.

    Scoped to the current task (a ContextVar): only a result saved inside the
    `with` block carries the intent; nothing outside it is affected.
    """
    token = _graph_intent_var.set(spec)
    try:
        yield
    finally:
        _graph_intent_var.reset(token)


async def _idempotent_hit(
    tenant_id: str,
    idempotency_key: str,
    fields: Mapping[str, Any],
    graph_intent: GraphIntentSpec | None,
) -> EnrichmentResult | None:
    """The stored result for this key, with its missing intent recorded, or None."""
    existing = await get_enrichment_result_by_idempotency_key(idempotency_key, tenant_id=tenant_id)
    if existing is None:
        return None
    logger.debug(
        "enrichment_result_idempotent_hit", idempotency_key=idempotency_key, tenant_id=tenant_id
    )
    if graph_intent is not None:
        async with get_session() as session:
            if await _intent_exists(session, graph_intent):
                # The operation committed with its obligation: this call is its
                # duplicate, whatever it recomputed.
                raise GraphIntentDuplicateError(graph_intent.tenant_id, graph_intent.effect_key)
            # A stored result with no obligation (older than graph_sync_intents,
            # or committed without a graph leg): backfill one only for a
            # verified replay -- never data the stored result does not hold.
            _require_verified_replay(existing, fields, graph_intent)
            await _insert_graph_intent(session, graph_intent, existing.id)
    return existing


async def _intent_exists(session: AsyncSession, spec: GraphIntentSpec) -> bool:
    found = await session.execute(
        select(GraphSyncIntent.id).where(
            GraphSyncIntent.tenant_id == spec.tenant_id,
            GraphSyncIntent.effect_key == spec.effect_key,
        )
    )
    return found.scalar_one_or_none() is not None


async def _idempotent_race_winner(
    session: AsyncSession,
    tenant_id: str,
    idempotency_key: str,
    fields: Mapping[str, Any],
    graph_intent: GraphIntentSpec | None,
) -> EnrichmentResult:
    """The concurrent save that won this key; the intent is rebound to it."""
    existing = (
        await session.execute(
            select(EnrichmentResult).where(
                EnrichmentResult.idempotency_key == idempotency_key,
                EnrichmentResult.tenant_id == tenant_id,
            )
        )
    ).scalar_one()
    if graph_intent is not None:
        # Raising rolls back the intent inserted earlier in this transaction.
        _require_verified_replay(existing, fields, graph_intent)
        await session.execute(
            update(GraphSyncIntent)
            .where(
                GraphSyncIntent.tenant_id == graph_intent.tenant_id,
                GraphSyncIntent.effect_key == graph_intent.effect_key,
            )
            .values(enrichment_result_id=existing.id)
        )
    logger.debug(
        "enrichment_result_idempotent_race", idempotency_key=idempotency_key, tenant_id=tenant_id
    )
    return existing


def _require_verified_replay(
    existing: EnrichmentResult, fields: Mapping[str, Any], spec: GraphIntentSpec
) -> None:
    """Attach an intent to a stored result only when this call replays it exactly.

    Same entity and committed fields, and every graph field the intent pins
    equal to the stored result's value for it -- a graph field the stored
    result does not hold as-is (shaped, or absent) cannot be verified. Anything
    else is a mismatch (`GraphIntentReplayMismatchError`): the stored result is
    the operation's, and this call's different result is not durable.
    """

    def normal(value: Any) -> str:
        return json.dumps(value, sort_keys=True, default=str)

    stored = dict(existing.fields or {})
    graph_verified = all(
        name in stored and normal(stored[name]) == normal(value)
        for name, value in spec.fields.items()
    )
    if (
        existing.entity_id != spec.entity_id
        or normal(stored) != normal(dict(fields))
        or not graph_verified
    ):
        logger.warning(
            "graph_intent_replay_mismatch",
            tenant_id=spec.tenant_id,
            effect_key=spec.effect_key,
            stored_entity_id=existing.entity_id,
            entity_id=spec.entity_id,
        )
        raise GraphIntentReplayMismatchError(spec.tenant_id, spec.effect_key)


async def _insert_graph_intent(
    session: AsyncSession, spec: GraphIntentSpec, result_id: uuid.UUID | None
) -> None:
    """Insert `spec` once; an existing row for its key makes this a duplicate.

    `ON CONFLICT DO NOTHING` waits for a concurrent uncommitted insert of the
    same key and then inserts nothing, so of two workers committing one
    operation exactly one row -- and one result -- survives.
    """
    stmt = (
        insert(GraphSyncIntent)
        .values(
            id=uuid.uuid4(),
            tenant_id=spec.tenant_id,
            effect_key=spec.effect_key,
            entity_id=spec.entity_id,
            node_label=spec.node_label,
            domain=spec.domain,
            endpoint=spec.endpoint,
            contract_digest=spec.contract_digest,
            cartridge_revision=spec.cartridge_revision,
            origin=spec.origin,
            fields=json.loads(json.dumps(dict(spec.fields), default=str)),
            enrichment_result_id=result_id,
            state=GRAPH_INTENT_PENDING,
            attempts=0,
            max_attempts=spec.max_attempts,
        )
        .on_conflict_do_nothing(constraint="uq_graph_sync_intents_tenant_key")
        .returning(GraphSyncIntent.id)
    )
    inserted = (await session.execute(stmt)).scalar_one_or_none()
    if inserted is None:
        raise GraphIntentDuplicateError(spec.tenant_id, spec.effect_key)
    logger.info(
        "graph_intent_recorded",
        tenant_id=spec.tenant_id,
        effect_key=spec.effect_key,
        entity_id=spec.entity_id,
        endpoint=spec.endpoint,
    )


def _intent_record(row: Any) -> GraphIntentRecord:
    m = row._mapping if hasattr(row, "_mapping") else row
    return GraphIntentRecord(
        id=m["id"],
        seq=int(m["seq"]),
        tenant_id=m["tenant_id"],
        effect_key=m["effect_key"],
        entity_id=m["entity_id"],
        node_label=m["node_label"],
        domain=m["domain"],
        endpoint=m["endpoint"],
        contract_digest=m["contract_digest"],
        cartridge_revision=m["cartridge_revision"],
        origin=m["origin"],
        fields=dict(m["fields"] or {}),
        enrichment_result_id=m["enrichment_result_id"],
        state=m["state"],
        attempts=int(m["attempts"]),
        max_attempts=int(m["max_attempts"]),
        next_attempt_at=m["next_attempt_at"],
        lease_owner=m["lease_owner"],
        lease_expires_at=m["lease_expires_at"],
        last_error=m["last_error"],
        last_ack=m["last_ack"],
    )


def _orm_intent_record(row: GraphSyncIntent) -> GraphIntentRecord:
    return _intent_record({c.name: getattr(row, c.name) for c in GraphSyncIntent.__table__.columns})


async def get_graph_intent(tenant_id: str, effect_key: str) -> GraphIntentRecord | None:
    """The intent of one operation, or None when it has none."""
    async with get_session() as session:
        row = (
            await session.execute(
                select(GraphSyncIntent).where(
                    GraphSyncIntent.tenant_id == tenant_id,
                    GraphSyncIntent.effect_key == effect_key,
                )
            )
        ).scalar_one_or_none()
    return _orm_intent_record(row) if row is not None else None


async def claim_graph_intents(
    *,
    owner: str,
    lease_seconds: float,
    limit: int = 50,
    tenant_id: str | None = None,
    effect_key: str | None = None,
    now: datetime | None = None,
    held_origins: frozenset[str] = frozenset(),
    max_apply_seconds: float = GRAPH_MAX_APPLY_SECONDS,
) -> list[GraphIntentRecord]:
    """Atomically lease due intents to `owner` (cross-process safe).

    Due: `pending` past `next_attempt_at`, or `dispatching` whose lease expired
    (the holder died mid-attempt). A row is skipped while an OLDER version of
    its subject is outstanding, and `FOR UPDATE SKIP LOCKED` keeps two workers
    from ever leasing one row. Before claiming, two kinds of row are resolved:

    * a `pending` row whose subject has a NEWER version already acknowledged or
      in flight is `superseded` -- sending it would overwrite newer graph state;
    * an expired lease on a row that spent `max_attempts` is `failed_terminal`
      (`retry_exhausted`) -- never left to loop.

    `tenant_id` + `effect_key` narrows the claim to one operation (the inline
    leg). Each claim counts as an attempt, and each call leases under a fresh
    fencing token (`lease_owner` = `owner#<token>`): only that claim can settle
    the row, even when the same process re-claims it after its lease expired.

    A subject is the graph row a version writes: tenant, entity and resolved
    endpoint. "Sent" means claimed at least once (`attempts > 0`), whatever the
    state since -- a newer version whose ack was lost is back to `pending` but
    may already be in the graph. An older version is never claimed once a
    newer one was sent, and is `superseded` instead.

    `held_origins` are left untouched -- neither claimed nor resolved -- while
    their workflow is switched off (e.g. converge graph sync disabled).

    At most one version of a subject is in flight. A version is also held while
    an older one is *uncertain* and resolved less than `max_apply_seconds` ago:
    a request of it may still be applied late (graph_sync_intents.md
    *Ordering*). Uncertain means sent and not settled by a single request that
    was acknowledged or definitively refused -- a version claimed more than
    once, retried by the transport inside one claim (`GRAPH_RETRIED_IN_CLAIM`),
    or ended `retry_exhausted`, may have a request CEG has not applied yet.
    """
    if (tenant_id is None) != (effect_key is None):
        msg = "claim one operation with both tenant_id and effect_key, or neither"
        raise ValueError(msg)
    at = now or datetime.now(UTC)
    claim_owner = f"{owner[:100]}#{uuid.uuid4().hex[:16]}"
    intent = GraphSyncIntent
    older_or_newer = aliased(GraphSyncIntent)

    def scope(table: Any) -> list[Any]:
        conditions: list[Any] = []
        if held_origins:
            conditions.append(table.origin.not_in(sorted(held_origins)))
        if tenant_id is not None:
            conditions += [table.tenant_id == tenant_id, table.effect_key == effect_key]
        return conditions

    def same_subject(subject: Any) -> Any:
        return and_(
            older_or_newer.tenant_id == subject.tenant_id,
            older_or_newer.entity_id == subject.entity_id,
            older_or_newer.endpoint.is_not_distinct_from(subject.endpoint),
        )

    apply_horizon = at - timedelta(seconds=max_apply_seconds)

    def uncertain(older: Any) -> Any:
        return and_(
            older.attempts > 0,
            or_(
                older.attempts > 1,
                older.state.not_in((GRAPH_INTENT_ACKNOWLEDGED, GRAPH_INTENT_FAILED_TERMINAL)),
                older.last_error.like("retry_exhausted%"),
                older.last_error.like(f"%{GRAPH_RETRIED_IN_CLAIM}%"),
            ),
        )

    def newer_sent(subject: Any) -> Any:
        return exists().where(
            same_subject(subject),
            older_or_newer.seq > subject.seq,
            older_or_newer.attempts > 0,
        )

    supersede = (
        update(intent)
        .where(
            intent.state == GRAPH_INTENT_PENDING,
            *scope(intent),
            newer_sent(intent),
        )
        .values(
            state=GRAPH_INTENT_SUPERSEDED,
            last_error="superseded_by_newer_version",
            resolved_at=at,
            updated_at=at,
        )
        .execution_options(synchronize_session=False)
    )
    exhaust = (
        update(intent)
        .where(
            intent.state == GRAPH_INTENT_DISPATCHING,
            intent.lease_expires_at <= at,
            intent.attempts >= intent.max_attempts,
            *scope(intent),
        )
        .values(
            state=GRAPH_INTENT_FAILED_TERMINAL,
            last_error="retry_exhausted:lease_expired",
            lease_owner=None,
            lease_expires_at=None,
            resolved_at=at,
            updated_at=at,
        )
        .execution_options(synchronize_session=False)
    )
    candidate = aliased(GraphSyncIntent)

    def claimable(row: Any) -> list[Any]:
        return [
            or_(
                and_(row.state == GRAPH_INTENT_PENDING, row.next_attempt_at <= at),
                and_(row.state == GRAPH_INTENT_DISPATCHING, row.lease_expires_at <= at),
            ),
            row.attempts < row.max_attempts,
            *scope(row),
            ~exists().where(
                same_subject(row),
                older_or_newer.seq < row.seq,
                older_or_newer.state.in_((GRAPH_INTENT_PENDING, GRAPH_INTENT_DISPATCHING)),
            ),
            # An older version whose request may still land: wait out its window.
            ~exists().where(
                same_subject(row),
                older_or_newer.seq < row.seq,
                uncertain(older_or_newer),
                older_or_newer.resolved_at > apply_horizon,
            ),
            # Repeated here, not only in `supersede`: under Read Committed an
            # older row committed after `supersede` ran is visible to this claim.
            ~newer_sent(row),
        ]

    def candidates_query(exclude: set[uuid.UUID], room: int) -> Any:
        query = select(
            candidate.id, candidate.tenant_id, candidate.entity_id, candidate.endpoint
        ).where(*claimable(candidate))
        if exclude:
            query = query.where(candidate.id.not_in(sorted(exclude)))
        return query.order_by(candidate.seq).limit(room)

    def claim_statement(candidate_id: uuid.UUID) -> Any:
        locked = aliased(GraphSyncIntent)
        return (
            update(intent)
            .where(
                intent.id.in_(
                    select(locked.id)
                    .where(locked.id == candidate_id, *claimable(locked))
                    .with_for_update(skip_locked=True)
                    .scalar_subquery()
                )
            )
            .values(
                state=GRAPH_INTENT_DISPATCHING,
                lease_owner=claim_owner,
                lease_expires_at=at + timedelta(seconds=lease_seconds),
                attempts=intent.attempts + 1,
                updated_at=at,
            )
            .returning(*GraphSyncIntent.__table__.columns)
            .execution_options(synchronize_session=False)
        )

    async with get_session() as session:
        await session.execute(supersede)
        await session.execute(exhaust)
        candidates = (await session.execute(candidates_query(set(), limit))).all()

    # One transaction per claim, serialised per subject: the advisory lock is
    # held until the claim commits, so a concurrent claim of the same subject
    # waits and then -- in a statement that starts after the commit -- sees
    # this claim's `attempts`. Without it, two Read Committed claims could each
    # miss the other's uncommitted lease and send two versions out of order.
    # A candidate another worker won (or that stopped being claimable) is not
    # the end of the scan: the window is refilled, excluding every candidate
    # already checked by this call, until `limit` rows are leased or none is
    # left. Each candidate is checked at most once, so the loop ends.
    rows: list[Any] = []
    checked: set[uuid.UUID] = set()
    while candidates:
        for candidate_id, subject_tenant, subject_entity, subject_endpoint in candidates:
            checked.add(candidate_id)
            subject = f"{subject_tenant}|{subject_entity}|{subject_endpoint or ''}"
            async with get_session() as session:
                await session.execute(
                    select(func.pg_advisory_xact_lock(func.hashtextextended(subject, 0)))
                )
                await session.execute(supersede)
                rows.extend((await session.execute(claim_statement(candidate_id))).all())
        if len(rows) >= limit:
            break
        async with get_session() as session:
            candidates = (await session.execute(candidates_query(checked, limit - len(rows)))).all()
    claimed = sorted((_intent_record(r) for r in rows), key=lambda r: r.seq)
    if claimed:
        logger.info("graph_intents_claimed", owner=claim_owner, count=len(claimed))
    return claimed


def graph_intent_backoff(attempts: int, *, base_seconds: float, cap_seconds: float) -> float:
    """Seconds before the next attempt after `attempts` failed ones."""
    delay: float = base_seconds * (2 ** max(attempts - 1, 0))
    return min(cap_seconds, delay)


async def settle_graph_intent(
    intent_id: uuid.UUID,
    *,
    owner: str,
    outcome: str,
    error: str | None = None,
    ack: Mapping[str, Any] | None = None,
    retry_base_seconds: float = 2.0,
    retry_cap_seconds: float = 300.0,
    now: datetime | None = None,
) -> str | None:
    """Record one attempt's outcome; returns the new state, None if the lease was lost.

    `owner` is the claim's fencing token (the claimed record's `lease_owner`).
    `outcome` is `acknowledged`, `retry` or `terminal`. Only that claim settles:
    one whose lease expired and was re-claimed -- by another worker or by this
    one -- settles nothing (the newer claim owns the attempt now). A `retry` past
    `max_attempts` becomes `failed_terminal` with `retry_exhausted`.
    """
    at = now or datetime.now(UTC)
    async with get_session() as session:
        row = (
            await session.execute(
                select(GraphSyncIntent)
                .where(
                    GraphSyncIntent.id == intent_id,
                    GraphSyncIntent.state == GRAPH_INTENT_DISPATCHING,
                    GraphSyncIntent.lease_owner == owner,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            logger.warning("graph_intent_settle_lease_lost", intent_id=str(intent_id), owner=owner)
            return None
        row.lease_owner = None
        row.lease_expires_at = None
        row.updated_at = at
        if outcome == "acknowledged":
            row.state = GRAPH_INTENT_ACKNOWLEDGED
            row.last_ack = json.loads(json.dumps(dict(ack or {}), default=str))
            row.last_error = error  # None, or why an earlier request is uncertain
            row.resolved_at = at
        elif outcome == "terminal":
            row.state = GRAPH_INTENT_FAILED_TERMINAL
            row.last_error = error
            row.resolved_at = at
        elif outcome == "retry":
            row.last_error = error
            if row.attempts >= row.max_attempts:
                row.state = GRAPH_INTENT_FAILED_TERMINAL
                row.last_error = f"retry_exhausted:{error}"
                row.resolved_at = at
            else:
                row.state = GRAPH_INTENT_PENDING
                row.next_attempt_at = at + timedelta(
                    seconds=graph_intent_backoff(
                        row.attempts,
                        base_seconds=retry_base_seconds,
                        cap_seconds=retry_cap_seconds,
                    )
                )
        else:
            msg = f"unknown graph intent outcome {outcome!r}"
            raise ValueError(msg)
        state = row.state
    log = logger.info if state == GRAPH_INTENT_ACKNOWLEDGED else logger.warning
    log("graph_intent_settled", intent_id=str(intent_id), state=state, error=error)
    return state


async def graph_intent_status_counts(tenant_id: str | None = None) -> dict[str, int]:
    """Operator view: how many intents are in each state (optionally per tenant)."""
    stmt = select(GraphSyncIntent.state, func.count()).group_by(GraphSyncIntent.state)
    if tenant_id is not None:
        stmt = stmt.where(GraphSyncIntent.tenant_id == tenant_id)
    async with get_session() as session:
        rows = (await session.execute(stmt)).all()
    return {state: int(count) for state, count in rows}


def settled_graph_fields(
    intents: Iterable[tuple[str, int, Mapping[str, Any]]],
) -> dict[str, dict[str, Any]]:
    """The labelled values the graph provably holds, per field (echo guard, G4).

    ``intents`` are one subject's ``(state, attempts, fields)`` in ``seq``
    order. A field is settled only when the latest intent carrying it is
    acknowledged and every intent carrying it that was ever sent (claimed at
    least once) ended acknowledged: an unacknowledged sent intent may still be
    applied by CEG later, so its field is never suppressed.
    """
    latest: dict[str, dict[str, Any]] = {}
    latest_acked: dict[str, bool] = {}
    uncertain: set[str] = set()
    for state, attempts, fields in intents:
        acked = state == GRAPH_INTENT_ACKNOWLEDGED
        for name, value in fields.items():
            if name.endswith("_provenance") and name[: -len("_provenance")] in fields:
                continue  # a companion travels with its value field
            labelled = {name: value}
            companion = f"{name}_provenance"
            if companion in fields:
                labelled[companion] = fields[companion]
            latest[name], latest_acked[name] = labelled, acked
            if attempts > 0 and not acked:
                uncertain.add(name)
    return {
        name: labelled
        for name, labelled in latest.items()
        if latest_acked[name] and name not in uncertain
    }


async def graph_sync_settled_fields(
    tenant_id: str, entity_id: str, endpoint: str
) -> dict[str, dict[str, Any]]:
    """:func:`settled_graph_fields` for one subject (tenant, entity, endpoint)."""
    stmt = (
        select(GraphSyncIntent.state, GraphSyncIntent.attempts, GraphSyncIntent.fields)
        .where(
            GraphSyncIntent.tenant_id == tenant_id,
            GraphSyncIntent.entity_id == entity_id,
            GraphSyncIntent.endpoint == endpoint,
        )
        .order_by(GraphSyncIntent.seq)
    )
    async with get_session() as session:
        rows = (await session.execute(stmt)).all()
    return settled_graph_fields((str(s), int(a or 0), dict(f or {})) for s, a, f in rows)


# ── Graph-return targets (A4) ─────────────────────────────────────────────
# CEG's graph-inference-result outputs, retained durably until a later
# eligible converge of their tenant and entity consumes them. Internal state
# only: no payload, action or domain-spec key carries any column.

GRAPH_RETURN_RETAINED = "retained"
GRAPH_RETURN_CONSUMING = "consuming"
GRAPH_RETURN_CONSUMED = "consumed"
GRAPH_RETURN_REJECTED = "rejected"
GRAPH_RETURN_EXPIRED = "expired"
GRAPH_RETURN_TERMINAL_STATES = frozenset(
    {GRAPH_RETURN_CONSUMED, GRAPH_RETURN_REJECTED, GRAPH_RETURN_EXPIRED}
)
#: States that hold a target a converge may still use, or already used.
GRAPH_RETURN_ACCEPTED_STATES = (
    GRAPH_RETURN_RETAINED,
    GRAPH_RETURN_CONSUMING,
    GRAPH_RETURN_CONSUMED,
)
GRAPH_RETURN_OPEN_STATES = (GRAPH_RETURN_RETAINED, GRAPH_RETURN_CONSUMING)
GRAPH_RETURN_DEFAULT_MAX_ATTEMPTS = 5


class GraphReturnTarget(Base):
    """One graph-inference output retained for a later converge."""

    __tablename__ = "graph_return_targets"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    seq: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), nullable=False, unique=True
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(256), nullable=False)
    field_name: Mapped[str] = mapped_column(String(128), nullable=False)
    rule: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    value_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_key: Mapped[str] = mapped_column(String(64), nullable=False)
    intake_key: Mapped[str] = mapped_column(String(64), nullable=False)
    producer_key: Mapped[str] = mapped_column(String(256), nullable=False)
    origin_packet_id: Mapped[str] = mapped_column(String(128), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default=GRAPH_RETURN_RETAINED)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=GRAPH_RETURN_DEFAULT_MAX_ATTEMPTS
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_by: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "intake_key", name="uq_graph_return_targets_tenant_intake"),
        Index("ix_graph_return_targets_entity", "tenant_id", "entity_id", "state", "seq"),
        Index("ix_graph_return_targets_fact", "tenant_id", "fact_key"),
    )


@dataclass(frozen=True, slots=True)
class GraphReturnIntake:
    """One output of one graph-inference-result packet, bound at intake.

    `rejection` is set for an output that is well-formed enough to identify
    (tenant, entity, field) but is not eligible: it is recorded `rejected`
    with that reason, never retained.
    """

    tenant_id: str
    entity_id: str
    field_name: str
    rule: str
    value: Any
    confidence: float
    producer_key: str
    origin_packet_id: str
    rejection: str | None = None

    @property
    def value_digest(self) -> str:
        return _digest(json.dumps(self.value, sort_keys=True, default=str, separators=(",", ":")))

    @property
    def fact_key(self) -> str:
        """The fact: tenant, entity, field, rule and value -- not who sent it."""
        return _digest(
            "|".join(
                (self.tenant_id, self.entity_id, self.field_name, self.rule, self.value_digest)
            )
        )

    @property
    def intake_key(self) -> str:
        """The producer's idempotency key plus the fact (A1 F-A1-07)."""
        return _digest(f"{self.producer_key}|{self.fact_key}")


@dataclass(frozen=True, slots=True)
class GraphReturnIntakeResult:
    """What one packet's intake recorded."""

    retained: int
    duplicates: int
    unchanged: int
    rejected: int


@dataclass(frozen=True, slots=True)
class GraphReturnRecord:
    """A snapshot of one `graph_return_targets` row."""

    id: uuid.UUID
    seq: int
    tenant_id: str
    entity_id: str
    field_name: str
    rule: str
    value: Any
    confidence: float
    producer_key: str
    origin_packet_id: str
    state: str
    attempts: int
    max_attempts: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    expires_at: datetime
    created_at: datetime


def _digest(text_value: str) -> str:
    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


def _return_record(row: Any) -> GraphReturnRecord:
    value = row.value
    return GraphReturnRecord(
        id=row.id,
        seq=row.seq,
        tenant_id=row.tenant_id,
        entity_id=row.entity_id,
        field_name=row.field_name,
        rule=row.rule,
        value=value.get("value") if isinstance(value, dict) else value,
        confidence=row.confidence,
        producer_key=row.producer_key,
        origin_packet_id=row.origin_packet_id,
        state=row.state,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


def _intake_lock(tenant_id: str) -> Any:
    """Serialise one tenant's intake: capacity and the unchanged-fact check."""
    return select(func.pg_advisory_xact_lock(func.hashtextextended(f"graph_return|{tenant_id}", 0)))


async def _unchanged_facts(
    session: AsyncSession, tenant_id: str, intakes: list[GraphReturnIntake]
) -> set[str]:
    """Fact keys an accepted row already holds with at least this confidence."""
    by_fact = {i.fact_key: i.confidence for i in intakes}
    if not by_fact:
        return set()
    rows = (
        await session.execute(
            select(GraphReturnTarget.fact_key, func.max(GraphReturnTarget.confidence))
            .where(
                GraphReturnTarget.tenant_id == tenant_id,
                GraphReturnTarget.fact_key.in_(sorted(by_fact)),
                GraphReturnTarget.state.in_(GRAPH_RETURN_ACCEPTED_STATES),
            )
            .group_by(GraphReturnTarget.fact_key)
        )
    ).all()
    return {fact for fact, best in rows if best >= by_fact[fact]}


def _packet_tenant(intakes: list[GraphReturnIntake]) -> str:
    """The one tenant every output of a packet shares."""
    tenants = {i.tenant_id for i in intakes}
    if len(tenants) != 1:
        raise ValueError("one packet's outputs share one tenant")
    return next(iter(tenants))


def _intake_row(
    intake: GraphReturnIntake,
    reason: str | None,
    *,
    at: datetime,
    ttl_seconds: float,
    max_attempts: int,
) -> dict[str, Any]:
    """The `graph_return_targets` row one output records: rejected with `reason`, else retained."""
    return {
        "id": uuid.uuid4(),
        "tenant_id": intake.tenant_id,
        "entity_id": intake.entity_id[:256],
        "field_name": intake.field_name[:128],
        "rule": intake.rule[:128],
        "value": json.loads(json.dumps({"value": intake.value}, default=str)),
        "value_digest": intake.value_digest,
        "fact_key": intake.fact_key,
        "intake_key": intake.intake_key,
        "producer_key": intake.producer_key[:256],
        "origin_packet_id": intake.origin_packet_id[:128],
        "confidence": intake.confidence,
        "state": GRAPH_RETURN_REJECTED if reason else GRAPH_RETURN_RETAINED,
        "reason": reason,
        "attempts": 0,
        "max_attempts": max_attempts,
        "expires_at": at + timedelta(seconds=ttl_seconds),
        "resolved_at": at if reason else None,
        "created_at": at,
        "updated_at": at,
    }


def _classify_fresh(
    fresh: list[GraphReturnIntake], unchanged: set[str]
) -> tuple[list[tuple[GraphReturnIntake, str | None]], int, int, int]:
    """Each new output with its rejection reason (None: retain), and the three counts.

    An eligible output whose fact an accepted row already holds is
    `unchanged_fact`; within one packet, a second output of a fact retained
    earlier in it is unchanged too.
    """
    seen = set(unchanged)
    decided: list[tuple[GraphReturnIntake, str | None]] = []
    retained = rejected = repeated = 0
    for intake in fresh:
        reason = intake.rejection
        if reason is not None:
            rejected += 1
        elif intake.fact_key in seen:
            reason = "unchanged_fact"
            repeated += 1
        else:
            retained += 1
            seen.add(intake.fact_key)
        decided.append((intake, reason))
    return decided, retained, rejected, repeated


async def _require_capacity(
    session: AsyncSession, tenant_id: str, incoming: int, capacity: int
) -> None:
    """Refuse, recording nothing, when `incoming` more open rows would pass `capacity`."""
    open_count = (
        await session.execute(
            select(func.count()).where(
                GraphReturnTarget.tenant_id == tenant_id,
                GraphReturnTarget.state.in_(GRAPH_RETURN_OPEN_STATES),
            )
        )
    ).scalar_one()
    if int(open_count) + incoming > capacity:
        raise GraphReturnCapacityError(tenant_id, int(open_count), incoming, capacity)


async def retain_graph_return_targets(
    intakes: list[GraphReturnIntake],
    *,
    capacity: int,
    ttl_seconds: float,
    max_attempts: int = GRAPH_RETURN_DEFAULT_MAX_ATTEMPTS,
    now: datetime | None = None,
) -> GraphReturnIntakeResult:
    """Record one packet's outputs atomically and idempotently.

    All outputs share the packet's tenant. In one transaction, under a
    per-tenant advisory lock:

    * an output whose intake key (producer key + fact) is already recorded is a
      duplicate -- a Gate retransmit or a restart replay -- and records nothing;
    * an eligible output whose fact an accepted row already holds with at
      least this confidence is `rejected` (`unchanged_fact`): an echo is not a
      new observation and must not reseed the field;
    * an ineligible output is `rejected` with its reason;
    * the rest are `retained`.

    When retaining them would take the tenant's open rows past `capacity`,
    nothing is recorded and `GraphReturnCapacityError` is raised: the packet is
    refused whole and retryable, never half-accepted.
    """
    if not intakes:
        return GraphReturnIntakeResult(retained=0, duplicates=0, unchanged=0, rejected=0)
    tenant_id = _packet_tenant(intakes)
    at = now or datetime.now(UTC)
    by_key: dict[str, GraphReturnIntake] = {}
    for intake in intakes:
        by_key.setdefault(intake.intake_key, intake)

    async with get_session() as session:
        await session.execute(_intake_lock(tenant_id))
        recorded_rows = (
            await session.execute(
                select(GraphReturnTarget.intake_key, GraphReturnTarget.state).where(
                    GraphReturnTarget.tenant_id == tenant_id,
                    GraphReturnTarget.intake_key.in_(sorted(by_key)),
                )
            )
        ).all()
        recorded: dict[str, str] = {str(key): str(state) for key, state in recorded_rows}
        fresh = [i for k, i in by_key.items() if k not in recorded]
        unchanged = await _unchanged_facts(
            session, tenant_id, [i for i in fresh if i.rejection is None]
        )
        decided, retained_new, rejected_new, unchanged_new = _classify_fresh(fresh, unchanged)
        if retained_new:
            await _require_capacity(session, tenant_id, retained_new, capacity)
        if decided:
            rows = [
                _intake_row(
                    intake, reason, at=at, ttl_seconds=ttl_seconds, max_attempts=max_attempts
                )
                for intake, reason in decided
            ]
            await session.execute(
                insert(GraphReturnTarget)
                .values(rows)
                .on_conflict_do_nothing(constraint="uq_graph_return_targets_tenant_intake")
            )
    already = sum(1 for state in recorded.values() if state in GRAPH_RETURN_ACCEPTED_STATES)
    result = GraphReturnIntakeResult(
        retained=retained_new + already,
        duplicates=len(intakes) - len(fresh),
        unchanged=unchanged_new,
        rejected=rejected_new,
    )
    logger.info(
        "graph_return_targets_recorded",
        tenant_id=tenant_id,
        retained=result.retained,
        duplicates=result.duplicates,
        unchanged=result.unchanged,
        rejected=result.rejected,
    )
    return result


async def claim_graph_return_targets(
    *,
    tenant_id: str,
    entity_id: str,
    owner: str,
    lease_seconds: float,
    limit: int = 500,
    now: datetime | None = None,
) -> list[GraphReturnRecord]:
    """Lease one entity's retained targets to one converge (cross-process safe).

    First, in the same transaction, the entity's rows that can no longer be
    consumed are `expired`: past their TTL, or leased `max_attempts` times
    without being settled. Then `FOR UPDATE SKIP LOCKED` leases the rest --
    `retained`, or `consuming` under an expired lease (the converge holding it
    died) -- under a fresh fencing token, so two workers never lease one row
    and only this claim can settle what it leased.
    """
    at = now or datetime.now(UTC)
    claim_owner = f"{owner[:100]}#{uuid.uuid4().hex[:16]}"
    target = GraphReturnTarget

    def entity(table: Any) -> list[Any]:
        return [table.tenant_id == tenant_id, table.entity_id == entity_id]

    def lease_free(table: Any) -> Any:
        return or_(
            table.state == GRAPH_RETURN_RETAINED,
            and_(table.state == GRAPH_RETURN_CONSUMING, table.lease_expires_at <= at),
        )

    expire_ttl = (
        update(target)
        .where(*entity(target), lease_free(target), target.expires_at <= at)
        .values(
            state=GRAPH_RETURN_EXPIRED,
            reason="ttl_expired",
            lease_owner=None,
            lease_expires_at=None,
            resolved_at=at,
            updated_at=at,
        )
        .execution_options(synchronize_session=False)
    )
    expire_attempts = (
        update(target)
        .where(*entity(target), lease_free(target), target.attempts >= target.max_attempts)
        .values(
            state=GRAPH_RETURN_EXPIRED,
            reason="attempts_exhausted",
            lease_owner=None,
            lease_expires_at=None,
            resolved_at=at,
            updated_at=at,
        )
        .execution_options(synchronize_session=False)
    )
    locked = aliased(GraphReturnTarget)
    claim = (
        update(target)
        .where(
            target.id.in_(
                select(locked.id)
                .where(*entity(locked), lease_free(locked), locked.expires_at > at)
                .order_by(locked.seq)
                .limit(limit)
                .with_for_update(skip_locked=True)
                .scalar_subquery()
            )
        )
        .values(
            state=GRAPH_RETURN_CONSUMING,
            lease_owner=claim_owner,
            lease_expires_at=at + timedelta(seconds=lease_seconds),
            attempts=target.attempts + 1,
            updated_at=at,
        )
        .returning(*GraphReturnTarget.__table__.columns)
        .execution_options(synchronize_session=False)
    )
    async with get_session() as session:
        await session.execute(expire_ttl)
        await session.execute(expire_attempts)
        rows = (await session.execute(claim)).all()
    claimed = sorted((_return_record(r) for r in rows), key=lambda r: r.seq)
    if claimed:
        logger.info(
            "graph_return_targets_claimed",
            tenant_id=tenant_id,
            entity_id=entity_id,
            owner=claim_owner,
            count=len(claimed),
        )
    return claimed


async def settle_graph_return_targets(
    *,
    owner: str,
    consumed: Mapping[uuid.UUID, str],
    rejected: Mapping[uuid.UUID, str],
    consumed_by: str | None,
    now: datetime | None = None,
) -> int:
    """Record what a converge did with the targets it leased; returns rows settled.

    `consumed` maps a row to its outcome (`seeded`, or why the pass kept another
    value); `rejected` maps a row to why the converge refused it. Only the claim
    holding `owner` (its fencing token) settles: a lease that expired and was
    re-claimed settles nothing.
    """
    at = now or datetime.now(UTC)
    settled = 0
    async with get_session() as session:
        for state, mapping, column in (
            (GRAPH_RETURN_CONSUMED, consumed, "outcome"),
            (GRAPH_RETURN_REJECTED, rejected, "reason"),
        ):
            for row_id, note in mapping.items():
                result = await session.execute(
                    update(GraphReturnTarget)
                    .where(
                        GraphReturnTarget.id == row_id,
                        GraphReturnTarget.state == GRAPH_RETURN_CONSUMING,
                        GraphReturnTarget.lease_owner == owner,
                    )
                    .values(
                        {
                            "state": state,
                            column: note[:64] if column == "outcome" else note,
                            "consumed_by": consumed_by[:512] if consumed_by else None,
                            "lease_owner": None,
                            "lease_expires_at": None,
                            "resolved_at": at,
                            "updated_at": at,
                        }
                    )
                    .returning(GraphReturnTarget.id)
                    .execution_options(synchronize_session=False)
                )
                settled += len(result.all())
    expected = len(consumed) + len(rejected)
    log = logger.info if settled == expected else logger.warning
    log("graph_return_targets_settled", owner=owner, settled=settled, expected=expected)
    return settled


async def graph_return_status_counts(tenant_id: str | None = None) -> dict[str, int]:
    """Operator view: how many graph-return targets are in each state."""
    stmt = select(GraphReturnTarget.state, func.count()).group_by(GraphReturnTarget.state)
    if tenant_id is not None:
        stmt = stmt.where(GraphReturnTarget.tenant_id == tenant_id)
    async with get_session() as session:
        rows = (await session.execute(stmt)).all()
    return {state: int(count) for state, count in rows}
