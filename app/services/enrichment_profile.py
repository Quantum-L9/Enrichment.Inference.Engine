"""Enrichment profiles — intelligent entity selection for batch convergence.

Nightly batch enrichment has no selection criteria. Running all 5,000 entities
every night wastes tokens. The profile selects the highest-impact entities and
allocates budget proportionally.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, Field

from app.utils.safe_convert import safe_float

logger = structlog.get_logger(__name__)


class SelectionMode(StrEnum):
    NIGHTLY_STALE = "nightly_stale"
    HIGH_NULL = "high_null"
    FAILED_MATCH = "failed_match"
    NEW_INTAKE = "new_intake"
    CUSTOM = "custom"


class SelectionCriteria(BaseModel):
    max_staleness_days: int = Field(default=30, ge=1)
    min_null_count: int = Field(default=3, ge=0)
    confidence_below: float = Field(default=0.70, ge=0.0, le=1.0)
    min_failed_matches: int = Field(default=1, ge=0)
    is_gate_critical_incomplete: bool = False


class EnrichmentProfile(BaseModel):
    profile_name: str
    mode: SelectionMode = SelectionMode.CUSTOM
    selection_criteria: SelectionCriteria = Field(default_factory=SelectionCriteria)
    batch_size: int = Field(default=100, ge=1, le=10000)
    max_budget_tokens: int = Field(default=50000, ge=1000)
    schedule_cron: str = "0 2 * * *"
    convergence_mode: bool = False
    max_passes: int = Field(default=3, ge=1, le=10)


class EntityRef(BaseModel):
    entity_id: str
    priority_score: float = 0.0
    null_count: int = 0
    staleness_days: int = 0
    avg_confidence: float = 0.0
    failed_matches: int = 0
    gate_fields_missing: int = 0


class EntityBudget(BaseModel):
    entity_id: str
    allocated_tokens: int = 0
    priority_score: float = 0.0
    suggested_variations: int = 3
    suggested_passes: int = 1


class EntityStore(Protocol):
    """Abstract interface for querying entity metadata."""

    def query_entities(
        self,
        max_staleness_days: int | None = None,
        min_null_count: int | None = None,
        confidence_below: float | None = None,
        min_failed_matches: int | None = None,
        gate_critical_incomplete: bool = False,
        limit: int = 1000,
    ) -> list[dict[str, Any]]: ...


W_NULL = 0.4
W_STALE = 0.3
W_CONF = 0.2
W_FAILED = 0.1

MIN_TOKENS_PER_ENTITY = 500
MAX_TOKENS_PER_ENTITY = 10000


def _compute_priority(entity: dict[str, Any]) -> float:
    null_count = int(entity.get("null_count", 0))
    staleness = int(entity.get("staleness_days", 0))
    confidence = safe_float(entity.get("avg_confidence", 1.0))
    failed = int(entity.get("failed_matches", 0))

    null_norm = min(null_count / 20.0, 1.0)
    stale_norm = min(staleness / 90.0, 1.0)
    conf_score = 1.0 - confidence
    failed_norm = min(failed / 10.0, 1.0)

    return round(
        (null_norm * W_NULL)
        + (stale_norm * W_STALE)
        + (conf_score * W_CONF)
        + (failed_norm * W_FAILED),
        4,
    )


def select_entities(profile: EnrichmentProfile, store: EntityStore) -> list[EntityRef]:
    """Query the entity store, apply selection criteria, rank by priority."""
    criteria = profile.selection_criteria
    raw = store.query_entities(
        max_staleness_days=criteria.max_staleness_days,
        min_null_count=criteria.min_null_count,
        confidence_below=criteria.confidence_below,
        min_failed_matches=criteria.min_failed_matches,
        gate_critical_incomplete=criteria.is_gate_critical_incomplete,
        limit=profile.batch_size * 2,
    )

    refs: list[EntityRef] = []
    for entity in raw:
        priority = _compute_priority(entity)
        refs.append(
            EntityRef(
                entity_id=str(entity.get("entity_id", entity.get("id", ""))),
                priority_score=priority,
                null_count=int(entity.get("null_count", 0)),
                staleness_days=int(entity.get("staleness_days", 0)),
                avg_confidence=safe_float(entity.get("avg_confidence", 0.0)),
                failed_matches=int(entity.get("failed_matches", 0)),
                gate_fields_missing=int(entity.get("gate_fields_missing", 0)),
            )
        )

    refs.sort(key=lambda r: r.priority_score, reverse=True)
    return refs[: profile.batch_size]


def allocate_budget(
    entities: list[EntityRef],
    max_budget_tokens: int,
) -> list[EntityBudget]:
    """Distribute tokens across selected entities proportional to priority."""
    if not entities:
        return []

    total_priority = sum(e.priority_score for e in entities)
    if total_priority <= 0:
        per_entity = max_budget_tokens // len(entities)
        per_entity = max(MIN_TOKENS_PER_ENTITY, min(per_entity, MAX_TOKENS_PER_ENTITY))
        return [
            EntityBudget(
                entity_id=e.entity_id,
                allocated_tokens=per_entity,
                priority_score=e.priority_score,
                suggested_variations=3,
                suggested_passes=1,
            )
            for e in entities
        ]

    budgets: list[EntityBudget] = []
    tokens_remaining = max_budget_tokens

    for entity in entities:
        share = entity.priority_score / total_priority
        raw_tokens = int(share * max_budget_tokens)
        clamped = max(
            MIN_TOKENS_PER_ENTITY, min(raw_tokens, MAX_TOKENS_PER_ENTITY, tokens_remaining)
        )
        tokens_remaining -= clamped

        variations = 3
        if entity.avg_confidence < 0.4:
            variations = 5
        elif entity.avg_confidence > 0.8:
            variations = 2

        passes = 1
        if entity.null_count > 10 or entity.gate_fields_missing > 2:
            passes = 3
        elif entity.null_count > 5:
            passes = 2

        budgets.append(
            EntityBudget(
                entity_id=entity.entity_id,
                allocated_tokens=clamped,
                priority_score=entity.priority_score,
                suggested_variations=variations,
                suggested_passes=passes,
            )
        )

        if tokens_remaining <= 0:
            break

    return budgets


DEFAULT_PROFILES: dict[str, EnrichmentProfile] = {
    "nightly_stale": EnrichmentProfile(
        profile_name="nightly_stale",
        mode=SelectionMode.NIGHTLY_STALE,
        selection_criteria=SelectionCriteria(max_staleness_days=30),
        batch_size=200,
        max_budget_tokens=100000,
        schedule_cron="0 2 * * *",
        convergence_mode=False,
    ),
    "high_null": EnrichmentProfile(
        profile_name="high_null",
        mode=SelectionMode.HIGH_NULL,
        selection_criteria=SelectionCriteria(min_null_count=5, confidence_below=0.50),
        batch_size=100,
        max_budget_tokens=80000,
        schedule_cron="0 3 * * *",
        convergence_mode=True,
        max_passes=3,
    ),
    "failed_match": EnrichmentProfile(
        profile_name="failed_match",
        mode=SelectionMode.FAILED_MATCH,
        selection_criteria=SelectionCriteria(min_failed_matches=2),
        batch_size=50,
        max_budget_tokens=50000,
        schedule_cron="0 4 * * 1",
        convergence_mode=True,
        max_passes=5,
    ),
    "new_intake": EnrichmentProfile(
        profile_name="new_intake",
        mode=SelectionMode.NEW_INTAKE,
        selection_criteria=SelectionCriteria(max_staleness_days=1),
        batch_size=500,
        max_budget_tokens=200000,
        schedule_cron="*/30 * * * *",
        convergence_mode=False,
    ),
}


class ProfileRegistry:
    """Loads and serves enrichment profiles from config or defaults."""

    __slots__ = ("_profiles",)

    def __init__(self, custom_profiles: dict[str, EnrichmentProfile] | None = None) -> None:
        self._profiles: dict[str, EnrichmentProfile] = dict(DEFAULT_PROFILES)
        if custom_profiles:
            self._profiles.update(custom_profiles)

    def get(self, name: str) -> EnrichmentProfile | None:
        return self._profiles.get(name)

    def list_profiles(self) -> list[str]:
        return sorted(self._profiles.keys())

    def register(self, profile: EnrichmentProfile) -> None:
        self._profiles[profile.profile_name] = profile
        logger.info("profile_registry.register: %s", profile.profile_name)

    def __len__(self) -> int:
        return len(self._profiles)
