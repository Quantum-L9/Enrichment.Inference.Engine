"""Tests for app/services/uncertainty_engine.py

Covers: variation budget from completeness, richness, and last_confidence.

Product API: compute_uncertainty(entity, target_schema=None, last_confidence=0.5,
max_variations=5) -> int in [2, max_variations].
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.uncertainty_engine import compute_uncertainty


@pytest.fixture
def rich_entity() -> dict[str, Any]:
    """Complete entity: 10+ filled fields so richness saturates at 1.0."""
    return {
        "Name": "Acme Recycling Corp",
        "BillingCountry": "US",
        "Industry": "Recycling",
        "polymer_type": "HDPE",
        "contamination_pct": 3.5,
        "facility_tier": "Tier 2",
        "mfi_range": "0.5-3.0",
        "material_grade": "Standard HDPE",
        "website": "https://acme.example",
        "city": "Houston",
        "state": "TX",
    }


class TestUncertaintyEngine:
    """Tests for the live variation-budget contract (int, not a float score)."""

    def test_empty_entity_high_uncertainty(self, sample_schema: dict[str, str]) -> None:
        budget = compute_uncertainty(entity={}, target_schema=sample_schema)
        assert isinstance(budget, int)
        assert budget >= 4
        assert budget <= 5

    def test_rich_entity_low_uncertainty(
        self, rich_entity: dict[str, Any], sample_schema: dict[str, str]
    ) -> None:
        budget = compute_uncertainty(entity=rich_entity, target_schema=sample_schema)
        assert isinstance(budget, int)
        assert 2 <= budget <= 3

    def test_low_confidence_increases_uncertainty(
        self, rich_entity: dict[str, Any], sample_schema: dict[str, str]
    ) -> None:
        low = compute_uncertainty(
            entity=rich_entity,
            target_schema=sample_schema,
            last_confidence=0.1,
        )
        high = compute_uncertainty(
            entity=rich_entity,
            target_schema=sample_schema,
            last_confidence=0.9,
        )
        assert isinstance(low, int)
        assert low > high

    def test_high_confidence_lowers_uncertainty(
        self, rich_entity: dict[str, Any], sample_schema: dict[str, str]
    ) -> None:
        high = compute_uncertainty(
            entity=rich_entity,
            target_schema=sample_schema,
            last_confidence=0.95,
        )
        low = compute_uncertainty(
            entity=rich_entity,
            target_schema=sample_schema,
            last_confidence=0.2,
        )
        assert isinstance(high, int)
        assert high < low
        assert high == 2

    def test_schema_coverage_penalty(
        self, rich_entity: dict[str, Any], sample_schema: dict[str, str]
    ) -> None:
        budget_partial = compute_uncertainty(
            entity={"polymer_type": "HDPE"},
            target_schema=sample_schema,
        )
        budget_full = compute_uncertainty(
            entity=rich_entity,
            target_schema=sample_schema,
        )
        assert isinstance(budget_partial, int)
        assert budget_partial > budget_full

    def test_uncertainty_returns_float(self) -> None:
        budget = compute_uncertainty(entity={"Name": "X"})
        assert isinstance(budget, int)
        assert 2 <= budget <= 5
