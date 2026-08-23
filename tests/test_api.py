"""
Tests — validates request parsing, auth, schema validation,
consensus math, and response structure.
Run: pytest tests/ -v
"""

from __future__ import annotations

import os

os.environ.update(
    {
        "PERPLEXITY_API_KEY": "test-key",
        "API_SECRET_KEY": "test-secret-key-32-chars-long!!",
        "API_KEY_HASH": "d74ff0ee8da3b9806b18c877dbf29bbde50b5bd8e4dad7a3a725000feb82e8f1",
        "KB_DIR": "./kb",
        "REDIS_URL": "redis://localhost:6379/0",
    }
)

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.consensus_engine import synthesize
from app.services.uncertainty_engine import compute_uncertainty
from app.services.validation_engine import ValidationError, validate_response

client = TestClient(app)
AUTH = {"X-API-Key": "pass"}


class TestHealth:
    def test_returns_200_no_auth(self):
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
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
