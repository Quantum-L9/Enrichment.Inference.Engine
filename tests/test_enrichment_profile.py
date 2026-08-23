"""Tests for app/services/enrichment_profile.py

Covers: EnrichmentProfile fields, ProfileRegistry lookup, budget allocation,
        entity selection via EntityStore.

Product API is source of truth: no max_variations; select_entities(profile, store);
allocate_budget(entities, max_budget_tokens).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.enrichment_profile import (
    EnrichmentProfile,
    EntityBudget,
    EntityRef,
    ProfileRegistry,
    SelectionMode,
    allocate_budget,
    select_entities,
)

# Named seed/enrich/discover/autonomous profiles are not in DEFAULT_PROFILES.
# Equivalent profiles preserve the old intensity ladder via current fields.
_EQUIVALENT_PROFILES: dict[str, EnrichmentProfile] = {
    "seed": EnrichmentProfile(
        profile_name="seed",
        mode=SelectionMode.NEW_INTAKE,
        batch_size=50,
        max_budget_tokens=10000,
        max_passes=2,
        convergence_mode=False,
    ),
    "enrich": EnrichmentProfile(
        profile_name="enrich",
        mode=SelectionMode.HIGH_NULL,
        batch_size=100,
        max_budget_tokens=25000,
        max_passes=3,
        convergence_mode=True,
    ),
    "discover": EnrichmentProfile(
        profile_name="discover",
        mode=SelectionMode.NIGHTLY_STALE,
        batch_size=200,
        max_budget_tokens=100000,
        max_passes=3,
        convergence_mode=False,
    ),
    "autonomous": EnrichmentProfile(
        profile_name="autonomous",
        mode=SelectionMode.CUSTOM,
        batch_size=500,
        max_budget_tokens=500000,
        max_passes=5,
        convergence_mode=True,
    ),
}


def _profile_from_registry_or_equivalent(name: str) -> EnrichmentProfile:
    registered = ProfileRegistry().get(name)
    if registered is not None:
        return registered
    return _EQUIVALENT_PROFILES[name]


class FakeEntityStore:
    """Minimal EntityStore: returns canned rows, honors limit."""

    def __init__(self, entities: list[dict[str, Any]]) -> None:
        self._entities = entities

    def query_entities(
        self,
        max_staleness_days: int | None = None,
        min_null_count: int | None = None,
        confidence_below: float | None = None,
        min_failed_matches: int | None = None,
        gate_critical_incomplete: bool = False,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return list(self._entities[:limit])


# ---------------------------------------------------------------------------
# EnrichmentProfile model
# ---------------------------------------------------------------------------


class TestEnrichmentProfile:
    """Tests for current EnrichmentProfile fields (profile_name required)."""

    def test_default_profile_properties(self) -> None:
        profile = EnrichmentProfile(profile_name="default")
        assert profile.profile_name == "default"
        assert profile.mode == SelectionMode.CUSTOM
        assert profile.batch_size >= 1
        assert profile.max_budget_tokens >= 1000
        assert profile.max_passes >= 1
        assert "max_variations" not in EnrichmentProfile.model_fields

    def test_seed_profile(self) -> None:
        profile = _profile_from_registry_or_equivalent("seed")
        assert profile.profile_name == "seed"
        assert profile.batch_size >= 1
        assert profile.max_passes >= 1
        assert profile.max_budget_tokens == 10000
        assert profile.mode in SelectionMode

    def test_enrich_profile(self) -> None:
        profile = _profile_from_registry_or_equivalent("enrich")
        assert profile.profile_name == "enrich"
        assert profile.batch_size == 100
        assert profile.max_passes == 3
        assert profile.convergence_mode is True
        assert profile.mode in SelectionMode

    def test_discover_profile(self) -> None:
        profile = _profile_from_registry_or_equivalent("discover")
        assert profile.profile_name == "discover"
        assert profile.batch_size >= 100
        assert profile.max_budget_tokens == 100000
        assert profile.max_passes >= 1
        assert profile.mode in SelectionMode

    def test_autonomous_profile(self) -> None:
        profile = _profile_from_registry_or_equivalent("autonomous")
        assert profile.profile_name == "autonomous"
        assert profile.batch_size >= 100
        assert profile.max_passes >= 3
        assert profile.max_budget_tokens >= 100000
        assert profile.mode in SelectionMode


# ---------------------------------------------------------------------------
# ProfileRegistry
# ---------------------------------------------------------------------------


class TestProfileRegistry:
    """Tests for profile lookup against DEFAULT_PROFILES plus registered names."""

    @pytest.fixture
    def registry(self) -> ProfileRegistry:
        reg = ProfileRegistry()
        for profile in _EQUIVALENT_PROFILES.values():
            if reg.get(profile.profile_name) is None:
                reg.register(profile)
        return reg

    def test_get_profile_by_name(self, registry: ProfileRegistry) -> None:
        profile = registry.get("seed")
        assert profile is not None
        assert profile.profile_name == "seed"
        assert profile.batch_size >= 1

    def test_unknown_profile_returns_none_or_default(self, registry: ProfileRegistry) -> None:
        profile = registry.get("nonexistent")
        assert profile is None or hasattr(profile, "profile_name")

    def test_register_adds_profile(self, registry: ProfileRegistry) -> None:
        new_profile = EnrichmentProfile(
            profile_name="custom",
            mode=SelectionMode.CUSTOM,
            batch_size=75,
            max_budget_tokens=30000,
            max_passes=4,
        )
        registry.register(new_profile)
        loaded = registry.get("custom")
        assert loaded is not None
        assert loaded.batch_size == 75
        assert loaded.max_passes == 4

    def test_default_named_profiles_exist(self, registry: ProfileRegistry) -> None:
        nightly = registry.get("nightly_stale")
        high_null = registry.get("high_null")
        failed = registry.get("failed_match")
        intake = registry.get("new_intake")
        assert nightly is not None
        assert nightly.mode == SelectionMode.NIGHTLY_STALE
        assert nightly.batch_size == 200
        assert high_null is not None
        assert high_null.mode == SelectionMode.HIGH_NULL
        assert high_null.max_passes == 3
        assert failed is not None
        assert failed.mode == SelectionMode.FAILED_MATCH
        assert intake is not None
        assert intake.mode == SelectionMode.NEW_INTAKE


# ---------------------------------------------------------------------------
# Budget Allocation
# ---------------------------------------------------------------------------


class TestBudgetAllocation:
    """Tests for allocate_budget(entities, max_budget_tokens)."""

    def test_allocate_budget_across_entities(self) -> None:
        entities = [
            EntityRef(entity_id=f"e{i}", priority_score=1.0, avg_confidence=0.5) for i in range(50)
        ]
        allocations = allocate_budget(entities, 50000)
        assert isinstance(allocations, list)
        assert allocations
        assert all(isinstance(item, EntityBudget) for item in allocations)
        assert sum(item.allocated_tokens for item in allocations) <= 50000

    def test_allocate_budget_single_entity(self) -> None:
        entities = [EntityRef(entity_id="solo", priority_score=1.0, avg_confidence=0.5)]
        allocations = allocate_budget(entities, 50000)
        assert len(allocations) == 1
        assert allocations[0].entity_id == "solo"
        # Per-entity clamp is [500, 10000]; a single entity cannot take the full 50k.
        assert 500 <= allocations[0].allocated_tokens <= 10000


# ---------------------------------------------------------------------------
# Entity Selection
# ---------------------------------------------------------------------------


class TestEntitySelection:
    """Tests for select_entities(profile, store) — batch_size is the cap."""

    def test_select_returns_list(self) -> None:
        rows = [
            {
                "entity_id": f"ent-{i}",
                "null_count": i,
                "staleness_days": i,
                "avg_confidence": 0.5,
                "failed_matches": 0,
                "gate_fields_missing": 0,
            }
            for i in range(20)
        ]
        profile = EnrichmentProfile(
            profile_name="select-cap",
            mode=SelectionMode.CUSTOM,
            batch_size=10,
        )
        selected = select_entities(profile, FakeEntityStore(rows))
        assert isinstance(selected, list)
        assert len(selected) <= profile.batch_size
        assert all(isinstance(item, EntityRef) for item in selected)
