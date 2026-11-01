"""
Tests — validates request parsing, auth, schema validation,
consensus math, and response structure.
Run: pytest tests/ -v
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.auth import DIRECT_TENANT_READ_ENVIRONMENTS
from app.core.config import get_settings
from app.core.exceptions import DependencyNotConfiguredError
from app.main import app
from app.services.consensus_engine import synthesize
from app.services.uncertainty_engine import compute_uncertainty
from app.services.validation_engine import ValidationError, validate_response

client = TestClient(app)
AUTH = {"X-API-Key": "pass"}

_MODULE_ENV = {
    "PERPLEXITY_API_KEY": "test-key",
    "API_SECRET_KEY": "test-secret-key-32-chars-long!!",
    "API_KEY_HASH": "d74ff0ee8da3b9806b18c877dbf29bbde50b5bd8e4dad7a3a725000feb82e8f1",
    "KB_DIR": "./kb",
    "REDIS_URL": "redis://localhost:6379/0",
}


@pytest.fixture(scope="module", autouse=True)
def _module_env() -> Iterator[None]:
    """Scope this module's environment to this module's tests.

    This was a module-level ``os.environ.update``, which pytest runs during
    collection and therefore for the whole session: every other module saw
    these values, REDIS_URL included. ``patch.dict`` restores the caller's
    environment on teardown, and the settings cache is cleared on both sides so
    neither this module nor the next reads a ``Settings`` built from the
    other's environment.
    """
    with patch.dict(os.environ, _MODULE_ENV):
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


class TestHealth:
    def test_returns_200_no_auth(self):
        """Liveness is unconditional; readiness is not.

        EIE-002: this asserted status == "ok". Nothing here runs the lifecycle,
        so registration has not been attempted — and with registration enabled
        (the default now, matching .env.example) an un-attempted registration is
        a node Gate holds no route to. That degrades readiness while the process
        is plainly alive, which is the distinction the endpoint now reports.
        """
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] in {"ok", "degraded"}
        assert body["gate_registration"] == "not_attempted"
        assert body["status"] == "degraded"
        assert body["version"] == "2.3.0"
        assert "circuit_breaker_state" in body

    def test_kb_status_fields_present(self):
        body = client.get("/api/v1/health").json()
        for key in ("kb_loaded", "kb_polymers", "kb_grades", "kb_rules"):
            assert key in body


class TestAuth:
    def test_missing_key_401(self):
        r = client.post(
            "/api/v1/enrich",
            json={
                "entity": {},
                "object_type": "Account",
                "objective": "test",
            },
        )
        assert r.status_code == 401

    def test_wrong_key_403(self):
        r = client.post(
            "/api/v1/enrich",
            json={
                "entity": {},
                "object_type": "Account",
                "objective": "test",
            },
            headers={"X-API-Key": "wrong"},
        )
        assert r.status_code == 403

    def test_valid_key_passes(self):
        r = client.post(
            "/api/v1/enrich",
            json={
                "entity": {"Name": "Test"},
                "object_type": "Account",
                "objective": "test",
                "max_variations": 2,
            },
            headers=AUTH,
        )
        assert r.status_code == 200


@pytest.fixture
def tenant_facing_environment(monkeypatch):
    """Run the chassis as staging: signed, tenant-facing, no direct tenant reads."""
    monkeypatch.setenv("L9_ENVIRONMENT", "staging")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestScoreRouterGuard:
    """EIE-SEC-02: /score is guarded on the router, before any provider resolves."""

    def test_missing_key_401(self):
        assert client.get("/score/profile/p1").status_code == 401

    def test_wrong_key_403(self):
        r = client.get("/score/profile/p1", headers={"X-API-Key": "wrong"})
        assert r.status_code == 403

    def test_valid_key_reaches_the_unconfigured_provider(self):
        # Auth passed; the next thing to run is the provider stub, which is the
        # documented not-yet-wired state of this router (TODO.md), not an auth gap.
        with pytest.raises(DependencyNotConfiguredError):
            client.get("/score/profile/p1", headers=AUTH)

    def test_post_routes_are_guarded_too(self):
        r = client.post(
            "/score/entity", json={"entity_id": "e", "scoring_profile_id": "p", "domain": "d"}
        )
        assert r.status_code == 401

    def test_direct_score_read_refused_outside_dev_environments(self, tenant_facing_environment):
        r = client.get("/score/profile/p1", headers=AUTH)
        assert r.status_code == 403
        assert "Gate ingress" in r.json()["detail"]

    def test_every_score_route_carries_the_guard(self):
        from app.core.auth import require_direct_tenant_read, verify_api_key
        from app.score.score_api import router

        for route in router.routes:
            calls = [d.call for d in route.dependant.dependencies]  # type: ignore[attr-defined]
            # Guards first: they resolve before any per-route provider stub.
            assert calls[:2] == [verify_api_key, require_direct_tenant_read], route.path  # type: ignore[attr-defined]


class TestTenantScopedReads:
    """EIE-SEC-03: a direct tenant-scoped HTTP read is key-authenticated, served
    only where the runtime is unsigned, and the tenant id must be legal."""

    def test_dev_environments_match_the_sdk_dev_mode_set(self):
        assert frozenset({"local", "dev", "test"}) == DIRECT_TENANT_READ_ENVIRONMENTS

    def test_fields_requires_key(self):
        assert client.get("/api/v1/fields/e1", params={"tenant_id": "acme"}).status_code == 401

    def test_fields_requires_tenant_id(self):
        assert client.get("/api/v1/fields/e1", headers=AUTH).status_code == 422

    def test_fields_rejects_illegal_tenant_id(self):
        r = client.get("/api/v1/fields/e1", params={"tenant_id": "Acme Corp"}, headers=AUTH)
        assert r.status_code == 400
        assert "tenant id" in r.json()["detail"]

    def test_fields_served_in_dev_environment_with_legal_tenant(self):
        store = AsyncMock()
        store.get_latest_for_entity.return_value = None
        with patch("app.api.v1.fields.ResultStore", return_value=store) as store_cls:
            r = client.get("/api/v1/fields/e1", params={"tenant_id": "acme"}, headers=AUTH)
        assert r.status_code == 404
        store_cls.assert_called_once_with(tenant_id="acme")

    def test_fields_refused_outside_dev_environments(self, tenant_facing_environment):
        with patch("app.api.v1.fields.ResultStore") as store_cls:
            r = client.get("/api/v1/fields/e1", params={"tenant_id": "acme"}, headers=AUTH)
        assert r.status_code == 403
        assert "Gate ingress" in r.json()["detail"]
        store_cls.assert_not_called()

    def test_field_history_refused_outside_dev_environments(self, tenant_facing_environment):
        r = client.get("/api/v1/fields/e1/f/history", params={"tenant_id": "acme"}, headers=AUTH)
        assert r.status_code == 403

    def test_proposals_scoped_by_tenant_in_dev(self):
        pending = AsyncMock(return_value=[])
        with patch("app.services.pg_store.get_pending_schema_proposals", pending):
            r = client.get(
                "/api/v1/proposals/plasticos", params={"tenant_id": "acme"}, headers=AUTH
            )
        assert r.status_code == 200
        assert r.json() == []
        pending.assert_awaited_once_with(tenant_id="acme", domain="plasticos")

    def test_proposals_refused_outside_dev_environments(self, tenant_facing_environment):
        pending = AsyncMock(return_value=[])
        with patch("app.services.pg_store.get_pending_schema_proposals", pending):
            r = client.get(
                "/api/v1/proposals/plasticos", params={"tenant_id": "acme"}, headers=AUTH
            )
        assert r.status_code == 403
        pending.assert_not_awaited()

    def test_proposals_rejects_illegal_tenant_id(self):
        r = client.get(
            "/api/v1/proposals/plasticos", params={"tenant_id": "systemdb"}, headers=AUTH
        )
        assert r.status_code == 400

    def test_discover_and_scan_refused_outside_dev_environments(self, tenant_facing_environment):
        discover = AsyncMock()
        with patch("app.api.v1.discover.handle_discover", discover):
            r = client.post(
                "/api/v1/discover",
                json={
                    "entity_id": "e",
                    "domain": "plasticos",
                    "object_type": "Account",
                    "tenant_id": "acme",
                },
                headers=AUTH,
            )
        assert r.status_code == 403
        discover.assert_not_awaited()
        r = client.post(
            "/api/v1/scan",
            json={"fields": [], "domain": "plasticos", "tenant_id": "acme"},
            headers=AUTH,
        )
        assert r.status_code == 403

    def test_discover_and_scan_still_require_the_key(self):
        assert client.post("/api/v1/discover", json={}).status_code == 401
        assert client.post("/api/v1/scan", json={}).status_code == 401

    def test_enrich_is_not_a_tenant_scoped_read(self, tenant_facing_environment):
        """/api/v1/enrich carries no tenant and stays key-only (unchanged contract)."""
        r = client.post(
            "/api/v1/enrich",
            json={
                "entity": {"Name": "Test"},
                "object_type": "Account",
                "objective": "t",
                "max_variations": 2,
            },
            headers=AUTH,
        )
        assert r.status_code == 200


class TestEnrich:
    def test_response_structure(self):
        r = client.post(
            "/api/v1/enrich",
            json={
                "entity": {"Name": "Acme Corp", "BillingCountry": "US"},
                "object_type": "Account",
                "schema": '{"Industry": "string", "Description": "string"}',
                "objective": "Classify industry",
                "max_variations": 2,
            },
            headers=AUTH,
        )
        assert r.status_code == 200
        body = r.json()
        assert "state" in body
        assert "processing_time_ms" in body
        assert "tokens_used" in body
        assert "kb_fragment_ids" in body

    def test_schema_as_dict(self):
        r = client.post(
            "/api/v1/enrich",
            json={
                "entity": {"Name": "Test"},
                "object_type": "Lead",
                "schema": {"Industry": "string"},
                "objective": "test",
                "max_variations": 2,
            },
            headers=AUTH,
        )
        assert r.status_code == 200


class TestBatch:
    def test_returns_structured(self):
        r = client.post(
            "/api/v1/enrich/batch",
            json={
                "entities": [
                    {
                        "entity": {"Name": "A"},
                        "object_type": "Account",
                        "objective": "test",
                        "max_variations": 2,
                    },
                    {
                        "entity": {"Name": "B"},
                        "object_type": "Account",
                        "objective": "test",
                        "max_variations": 2,
                    },
                ]
            },
            headers=AUTH,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        assert "total_tokens_used" in body

    def test_rejects_over_50(self):
        r = client.post(
            "/api/v1/enrich/batch",
            json={
                "entities": [
                    {"entity": {"Name": f"Co{i}"}, "object_type": "Account", "objective": "t"}
                    for i in range(51)
                ]
            },
            headers=AUTH,
        )
        assert r.status_code == 422


class TestValidationEngine:
    def test_schema_coercion(self):
        result = validate_response(
            {"fields": {"Industry": "Plastics", "revenue": "5000000"}, "confidence": 0.9},
            {"Industry": "string", "revenue": "float"},
        )
        assert result["Industry"] == "Plastics"
        assert result["revenue"] == 5000000.0
        assert result["confidence"] == 0.9

    def test_partial_accept(self):
        result = validate_response(
            {"fields": {"good_field": "value", "bad_field": "not_a_number"}, "confidence": 0.8},
            {"good_field": "string", "bad_field": "integer"},
        )
        assert "good_field" in result

    def test_list_preserves_order(self):
        result = validate_response(
            {"fields": {"tags": ["B", "A", "C", "A"]}, "confidence": 0.7},
            {"tags": "list"},
        )
        assert result["tags"] == ["B", "A", "C"]

    def test_not_dict_raises(self):
        with pytest.raises(ValidationError):
            validate_response("not a dict", None)


class TestConsensusEngine:
    def test_perfect_agreement(self):
        payloads = [
            {"confidence": 0.9, "Industry": "Recycling"},
            {"confidence": 0.85, "Industry": "Recycling"},
            {"confidence": 0.88, "Industry": "Recycling"},
        ]
        result = synthesize(payloads, 0.65, total_attempted=len(payloads))
        assert "Industry" in result["fields"]
        assert result["confidence"] > 0.8

    def test_single_payload_penalty(self):
        result = synthesize(
            [{"confidence": 0.9, "Industry": "Tech"}],
            0.65,
            total_attempted=5,
        )
        assert result["confidence"] < 0.5

    def test_no_consensus(self):
        payloads = [
            {"confidence": 0.3, "Industry": "A"},
            {"confidence": 0.3, "Industry": "B"},
            {"confidence": 0.3, "Industry": "C"},
        ]
        result = synthesize(payloads, 0.65)
        assert result["fields"] == {} or result["confidence"] < 0.65


class TestUncertaintyEngine:
    def test_rich_entity_low_variations(self):
        entity = {f"field_{i}": f"value_{i}" for i in range(15)}
        score = compute_uncertainty(entity, max_variations=5)
        assert 2 <= score <= 5

    def test_empty_entity_high_variations(self):
        score = compute_uncertainty({}, {"a": "string", "b": "string"}, max_variations=5)
        assert score >= 3

    def test_floor_is_2(self):
        entity = {f"f{i}": f"v{i}" for i in range(20)}
        score = compute_uncertainty(entity, max_variations=5, last_confidence=0.99)
        assert score >= 2
