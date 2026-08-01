"""Tier-2 contract: convergence loop consumes inference unlock_map for target order.

Contract-bound surface: app/engines/convergence_controller.py
Authority: unlock_map from inference_bridge_v2 must reorder (never add/drop) Pass 2+
search targets so highest-leverage missing fields are searched first.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.engines.convergence_controller import (
    _classification_cache,
    run_convergence_loop,
)
from app.models.schemas import EnrichRequest, EnrichResponse

pytestmark = [pytest.mark.unit]


UNLOCK_DOMAIN_SPEC = {
    "domain": "unlock_test",
    "ontology": {
        "nodes": {
            "Facility": {
                "properties": {
                    "polymers_handled": {"type": "list"},
                    "certifications": {"type": "list"},
                    "equipment_types": {"type": "list"},
                    "material_grade": {
                        "type": "string",
                        "managed_by": "inference",
                        "derived_from": ["polymers_handled", "certifications"],
                        "confidence_floor": 0.6,
                    },
                    "recyclability_score": {
                        "type": "string",
                        "managed_by": "inference",
                        "derived_from": ["polymers_handled"],
                        "confidence_floor": 0.6,
                    },
                }
            }
        }
    },
}


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.perplexity_api_key = "test-key"
    settings.perplexity_model = "sonar"
    settings.max_concurrent_variations = 3
    settings.default_timeout_seconds = 30
    settings.max_budget_tokens = 30000
    return settings


@pytest.mark.asyncio
async def test_contract_pass2_targets_ordered_by_unlock_value(mock_settings):
    """Pass 2 target order must reflect inference unlock values (contract)."""
    seen_orders: list[list[str]] = []

    async def enricher(request, settings, kb_resolver, idem_store, sonar_config=None):
        seen_orders.append(list((request.schema_ or {}).keys()))
        low = 0.5
        return EnrichResponse(
            fields={
                "polymers_handled": ["HDPE"],
                "certifications": ["ISO 9001"],
                "equipment_types": ["shredder"],
            },
            confidence=low,
            variation_count=2,
            pass_count=1,
            inference_version="test",
            processing_time_ms=50,
            tokens_used=100,
            state="completed",
            feature_vector={
                "per_field_confidence": {
                    "polymers_handled": low,
                    "certifications": low,
                    "equipment_types": low,
                }
            },
        )

    _classification_cache.clear()
    request = EnrichRequest(
        entity={"name": "Acme Recycling", "website": None},
        object_type="Facility",
        objective="Enrich facility profile",
    )
    await run_convergence_loop(
        request=request,
        settings=mock_settings,
        kb_resolver=MagicMock(),
        enricher=enricher,
        inference_rules=[],
        domain_spec=UNLOCK_DOMAIN_SPEC,
    )

    assert len(seen_orders) >= 2, "convergence loop must run at least two passes"
    pass2 = seen_orders[1]
    for field in ("polymers_handled", "certifications"):
        assert field in pass2, f"expected {field} among Pass 2 targets: {pass2}"
    assert pass2.index("polymers_handled") < pass2.index("certifications")
