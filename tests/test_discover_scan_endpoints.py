"""
Regression tests for the discover/scan REST endpoints (app/api/v1/discover.py).

Covers two functional bugs:
  1. POST /api/v1/discover imported a non-existent `discover()` and awaited it;
     the canonical interface is SchemaDiscoveryEngine, driven by handle_discover.
  2. POST /api/v1/scan awaited scan_crm_fields with the wrong signature; the
     canonical contract is synchronous scan_crm_fields(crm_fields, domain_spec).

Run: pytest tests/test_discover_scan_endpoints.py -v
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.v1.converge as converge_mod
import app.api.v1.discover as discover_mod
import app.engines.schema_discovery as schema_discovery_mod
from app.core.auth import verify_api_key
from app.services.crm_field_scanner import scan_crm_fields as service_scan_crm_fields

DOMAIN_SPEC = {
    "domain": {"id": "plastics-recycling", "name": "Plastics Recycling", "version": "1.0.0"},
    "ontology": {
        "nodes": [
            {
                "label": "Partner",
                "properties": {
                    "polymer_type": {"type": "string"},
                    "contamination_pct": {"type": "float"},
                    "facility_tier": {"type": "string"},
                },
            }
        ]
    },
}


@pytest.fixture
def client() -> TestClient:
    """Isolated app with only the discover router; auth stubbed, no lifespan."""
    app = FastAPI()
    app.include_router(discover_mod.router)
    app.dependency_overrides[verify_api_key] = lambda: None
    return TestClient(app)


# ── Canonical-interface regression guards ────────────────────────────────────


def test_schema_discovery_exposes_engine_not_module_function() -> None:
    # The invalid import was `from ...engines.schema_discovery import discover`.
    assert not hasattr(schema_discovery_mod, "discover")
    assert hasattr(schema_discovery_mod, "SchemaDiscoveryEngine")


def test_service_scan_is_synchronous() -> None:
    # discover.py wrongly awaited this; it must stay a plain sync function.
    assert not inspect.iscoroutinefunction(service_scan_crm_fields)


# ── POST /api/v1/discover ────────────────────────────────────────────────────


def test_discover_delegates_to_handle_discover(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = {"enrichment": {"fields": {}}, "schema_proposal": {"stage": "discovered"}}
    mock = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(discover_mod, "handle_discover", mock)

    resp = client.post(
        "/api/v1/discover",
        json={
            "entity_id": "e-1",
            "domain": "plastics-recycling",
            "object_type": "Account",
            "tenant_id": "tenant-1",
        },
    )

    assert resp.status_code == 200
    assert resp.json() == sentinel
    mock.assert_awaited_once()
    kwargs = mock.await_args.kwargs
    assert kwargs["tenant"] == "tenant-1"
    payload = kwargs["payload"]
    assert payload["entity_id"] == "e-1"
    assert payload["object_type"] == "Account"
    assert payload["domain"] == "plastics-recycling"
    assert payload["entity"] == {"id": "e-1"}
    assert payload["objective"]  # a non-empty enrichment objective is synthesized


def test_discover_maps_handler_error_to_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        discover_mod, "handle_discover", AsyncMock(side_effect=RuntimeError("boom"))
    )
    resp = client.post(
        "/api/v1/discover",
        json={
            "entity_id": "e-1",
            "domain": "plastics-recycling",
            "object_type": "Account",
            "tenant_id": "tenant-1",
        },
    )
    assert resp.status_code == 500
    assert "boom" in resp.json()["detail"]


# ── POST /api/v1/scan ────────────────────────────────────────────────────────


def test_scan_synchronous_contract_and_classification(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(converge_mod, "_domain_specs", {"plastics-recycling": DOMAIN_SPEC})

    resp = client.post(
        "/api/v1/scan",
        json={
            "fields": [
                {"name": "polymer_type", "type": "string"},
                {"name": "legacy_notes", "type": "string"},
            ],
            "domain": "plastics-recycling",
            "tenant_id": "tenant-1",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["domain_id"] == "plastics-recycling"
    assert data["matched_count"] == 1  # polymer_type
    assert data["unmapped_count"] == 1  # legacy_notes
    assert data["missing_count"] == 2  # contamination_pct, facility_tier
    matched_props = {m["domain_property"] for m in data["matched"]}
    assert matched_props == {"polymer_type"}


def test_scan_unknown_domain_returns_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(converge_mod, "_domain_specs", {"plastics-recycling": DOMAIN_SPEC})
    resp = client.post(
        "/api/v1/scan",
        json={"fields": [], "domain": "does-not-exist", "tenant_id": "tenant-1"},
    )
    assert resp.status_code == 400
    assert "does-not-exist" in resp.json()["detail"]
