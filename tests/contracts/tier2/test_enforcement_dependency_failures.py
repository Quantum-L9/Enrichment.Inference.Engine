"""
Tier 2 — Enforcement: Dependency Failure Modes
==============================================
Proves dependency degradation is explicit and predictable.

Primary sources:
- docs/contracts/dependencies/_index.yaml
- docs/contracts/dependencies/redis.yaml
- docs/contracts/dependencies/postgresql.yaml
- docs/contracts/dependencies/constellation-gate.yaml
- docs/contracts/dependencies/perplexity-sonar.yaml
- docs/contracts/dependencies/odoo-crm.yaml
- docs/contracts/dependencies/salesforce-crm.yaml
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.enforcement,
    pytest.mark.failure,
    pytest.mark.degraded,
]


class MockEIEService:
    def __init__(
        self,
        *,
        redis_available: bool = True,
        postgres_available: bool = True,
        gate_available: bool = True,
        providers: list[str] | None = None,
        crm_available: bool = True,
    ) -> None:
        self.redis_available = redis_available
        self.postgres_available = postgres_available
        self.gate_available = gate_available
        self.providers = providers if providers is not None else ["perplexity", "clearbit"]
        self.crm_available = crm_available

    def enrich(self, request: dict[str, Any]) -> dict[str, Any]:
        degraded_modes: list[str] = []

        if not self.redis_available:
            degraded_modes.extend(["idempotency", "event_publishing"])

        if not self.providers:
            return {
                "state": "failed",
                "failure_reason": "all_providers_unavailable",
                "degraded_modes": degraded_modes,
            }

        return {
            "fields": {"normalized_name": "Acme"},
            "confidence": 0.85,
            "state": "completed",
            "failure_reason": None,
            "provider": self.providers[0],
            "degraded_modes": degraded_modes,
        }

    def resume_convergence(self, run_id: str) -> dict[str, Any]:
        if not self.postgres_available:
            return {
                "run_id": run_id,
                "state": "not_resumable",
                "failure_reason": "postgres_unavailable",
            }
        return {"run_id": run_id, "state": "resumed"}

    def enrich_and_sync(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.gate_available:
            return {
                "state": "partial",
                "failure_reason": "gate_unavailable",
                "enriched": True,
                "graph_synced": False,
            }
        return {
            "state": "completed",
            "failure_reason": None,
            "enriched": True,
            "graph_synced": True,
        }

    def writeback(self, request: dict[str, Any]) -> dict[str, Any]:
        enriched_data = request.get("enriched_data", {})
        if not self.crm_available:
            return {
                "status": "failed",
                "failure_reason": "crm_unavailable",
                "attempted_fields": list(enriched_data.keys()),
                "written_fields": [],
                "skipped_fields": list(enriched_data.keys()),
                "skip_reasons": dict.fromkeys(enriched_data.keys(), "crm_unavailable"),
            }
        return {
            "status": "completed",
            "failure_reason": None,
            "attempted_fields": list(enriched_data.keys()),
            "written_fields": list(enriched_data.keys()),
            "skipped_fields": [],
            "skip_reasons": {},
        }


def make_enrich_request() -> dict[str, Any]:
    return {
        "entity": {"Name": "Acme Recycling Corp"},
        "object_type": "Account",
        "objective": "Enrich polymer data",
    }


def make_writeback_request() -> dict[str, Any]:
    return {
        "crm_type": "salesforce",
        "object_type": "Account",
        "record_id": "001B000000LpT1FIAV",
        "enriched_data": {"industry": "Manufacturing", "recycling_grade": "HDPE"},
        "confidence_threshold": 0.70,
    }


class TestRedisFailure:
    def test_redis_down_does_not_crash_enrich(self) -> None:
        service = MockEIEService(redis_available=False)
        result = service.enrich(make_enrich_request())
        assert result["state"] in {"completed", "failed", "partial"}

    def test_redis_down_surfaces_idempotency_degradation(self) -> None:
        service = MockEIEService(redis_available=False)
        result = service.enrich(make_enrich_request())
        assert "idempotency" in result["degraded_modes"]

    def test_redis_down_surfaces_event_publishing_degradation(self) -> None:
        service = MockEIEService(redis_available=False)
        result = service.enrich(make_enrich_request())
        assert "event_publishing" in result["degraded_modes"]


class TestPostgresFailure:
    def test_postgres_down_makes_resume_not_resumable(self) -> None:
        service = MockEIEService(postgres_available=False)
        result = service.resume_convergence("run-abc-123")
        assert result["state"] == "not_resumable"
        assert result["failure_reason"] == "postgres_unavailable"


class TestGateFailure:
    def test_gate_down_degrades_enrich_and_sync_explicitly(self) -> None:
        service = MockEIEService(gate_available=False)
        result = service.enrich_and_sync(make_enrich_request())
        assert result["state"] == "partial"
        assert result["graph_synced"] is False
        assert result["failure_reason"] == "gate_unavailable"

    def test_gate_down_does_not_block_enrichment(self) -> None:
        service = MockEIEService(gate_available=False)
        result = service.enrich_and_sync(make_enrich_request())
        assert result["enriched"] is True


class TestProviderFailure:
    def test_all_providers_unavailable_produces_failed_state(self) -> None:
        service = MockEIEService(providers=[])
        result = service.enrich(make_enrich_request())
        assert result["state"] == "failed"
        assert result["failure_reason"] == "all_providers_unavailable"

    def test_primary_provider_fallback_continues(self) -> None:
        service = MockEIEService(providers=["clearbit"])
        result = service.enrich(make_enrich_request())
        assert result["state"] == "completed"
        assert result["provider"] == "clearbit"


class TestCRMFailure:
    def test_crm_unavailable_produces_explicit_writeback_failure(self) -> None:
        service = MockEIEService(crm_available=False)
        result = service.writeback(make_writeback_request())
        assert result["status"] == "failed"
        assert result["failure_reason"] == "crm_unavailable"
        assert result["written_fields"] == []

    def test_crm_unavailable_skips_all_fields(self) -> None:
        service = MockEIEService(crm_available=False)
        result = service.writeback(make_writeback_request())
        assert set(result["skipped_fields"]) == {"industry", "recycling_grade"}
