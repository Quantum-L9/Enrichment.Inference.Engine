"""Durable loop state store — survives crashes, supports resume from last checkpoint."""

from __future__ import annotations

import abc
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog
from pydantic import BaseModel, Field

from ...models.field_confidence import FieldConfidenceMap
from ...models.loop_schemas import CostSummary, PassResult

logger = structlog.get_logger(__name__)

DEFAULT_TTL_SECONDS = 86400  # 24h


class LoopStatus(StrEnum):
    RUNNING = "running"
    CONVERGED = "converged"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MAX_PASSES = "max_passes"
    DIMINISHING_RETURNS = "diminishing_returns"
    HUMAN_HOLD = "human_hold"
    FAILED = "failed"


class LoopState(BaseModel):
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    entity_id: str = ""
    domain: str = ""
    # Nulls decode historical runs for diagnosis, never authorize approval.
    node_label: str | None = None
    discovery_spec_sha256: str | None = None
    ownership_contract_version: int | None = None
    discovery_field_confidences: dict[str, Any] = Field(default_factory=dict)
    observed_field_sources: dict[str, list[str]] = Field(default_factory=dict)
    status: LoopStatus = LoopStatus.RUNNING
    current_pass: int = 0
    passes_completed: list[PassResult] = Field(default_factory=list)
    accumulated_fields: dict[str, Any] = Field(default_factory=dict)
    accumulated_confidences: FieldConfidenceMap = Field(default_factory=FieldConfidenceMap)
    cost_summary: CostSummary = Field(default_factory=CostSummary)
    failure_reason: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)


class LoopStateStore(abc.ABC):
    @abc.abstractmethod
    async def save(self, state: LoopState) -> None: ...

    @abc.abstractmethod
    async def load(self, run_id: str) -> LoopState | None: ...

    @abc.abstractmethod
    async def list_active(self, domain: str | None = None) -> list[LoopState]: ...

    async def resume(self, run_id: str) -> LoopState | None:
        state = await self.load(run_id)
        if state is None:
            return None
        if state.status != LoopStatus.RUNNING:
            logger.info(
                "loop_state.resume_skipped",
                run_id=run_id,
                status=state.status.value,
            )
            return None
        logger.info(
            "loop_state.resumed",
            run_id=run_id,
            current_pass=state.current_pass,
            fields=len(state.accumulated_fields),
        )
        return state


class RedisLoopStateStore(LoopStateStore):
    """Fast ephemeral store backed by Redis with configurable TTL."""

    _PREFIX = "enrich:loop:"

    def __init__(self, redis_client: Any, ttl: int = DEFAULT_TTL_SECONDS) -> None:
        self._redis = redis_client
        self._ttl = ttl

    async def save(self, state: LoopState) -> None:
        state.touch()
        key = f"{self._PREFIX}{state.run_id}"
        payload = state.model_dump_json()
        await self._redis.set(key, payload, ex=self._ttl)

    async def load(self, run_id: str) -> LoopState | None:
        key = f"{self._PREFIX}{run_id}"
        raw = await self._redis.get(key)
        if raw is None:
            return None
        return LoopState.model_validate_json(raw)

    async def list_active(self, domain: str | None = None) -> list[LoopState]:
        pattern = f"{self._PREFIX}*"
        keys = []
        async for key in self._redis.scan_iter(match=pattern, count=100):
            keys.append(key)
        states: list[LoopState] = []
        for key in keys:
            raw = await self._redis.get(key)
            if raw is None:
                continue
            state = LoopState.model_validate_json(raw)
            if state.status not in {LoopStatus.RUNNING, LoopStatus.HUMAN_HOLD}:
                continue
            if domain and state.domain != domain:
                continue
            states.append(state)
        return states


class PostgresLoopStateStore(LoopStateStore):
    """Durable queryable store backed by PostgreSQL convergence_runs table."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def save(self, state: LoopState) -> None:
        state.touch()
        payload = state.model_dump_json()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO convergence_runs (run_id, entity_id, domain, status, payload, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
                ON CONFLICT (run_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    payload = EXCLUDED.payload,
                    updated_at = EXCLUDED.updated_at
                """,
                state.run_id,
                state.entity_id,
                state.domain,
                state.status.value,
                payload,
                state.created_at,
                state.updated_at,
            )

    async def load(self, run_id: str) -> LoopState | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT payload FROM convergence_runs WHERE run_id = $1", run_id
            )
        if row is None:
            return None
        return LoopState.model_validate_json(row["payload"])

    async def list_active(self, domain: str | None = None) -> list[LoopState]:
        query = "SELECT payload FROM convergence_runs WHERE status IN ('running', 'human_hold')"
        args: list[Any] = []
        if domain:
            query += " AND domain = $1"
            args.append(domain)
        query += " ORDER BY updated_at DESC LIMIT 500"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
        return [LoopState.model_validate_json(r["payload"]) for r in rows]
