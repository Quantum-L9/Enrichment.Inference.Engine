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
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.v1.converge as converge_mod
import app.api.v1.discover as discover_mod
import app.engines.schema_discovery as schema_discovery_mod
from app.core.auth import verify_api_key
from app.core.config import Settings
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
    # 500 with a populated detail — do not pin the raw exception text, which may
    # later be hardened to a generic message while the cause is still logged.
    assert resp.status_code == 500
    assert resp.json().get("detail")


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


def test_scan_unknown_domain_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(converge_mod, "_domain_specs", {"plastics-recycling": DOMAIN_SPEC})
    resp = client.post(
        "/api/v1/scan",
        json={"fields": [], "domain": "does-not-exist", "tenant_id": "tenant-1"},
    )
    assert resp.status_code == 404
    assert "does-not-exist" in resp.json()["detail"]


# ── POST /api/v1/scan — live Odoo source ─────────────────────────────────────

ODOO_URL = "https://odoo.test"
ODOO_API_KEY = "test-api-key"

PLASTICOS_SPEC = {
    "domain": {"id": "plasticos", "name": "PlasticOS", "version": "1.0.0"},
    "ontology": {
        "nodes": [
            {
                "label": "Partner",
                "properties": {
                    "name": {"type": "string"},
                    "phone": {"type": "string"},
                    "email": {"type": "string"},
                    "materials_handled": {"type": "string"},
                },
            }
        ]
    },
}

ODOO_PARTNER_FIELDS = {
    "name": {"string": "Name", "type": "char"},
    "phone": {"string": "Phone", "type": "char"},
    "email": {"string": "Email", "type": "char"},
    "x_materials_handled": {"string": "Materials Handled", "type": "char"},
}
ODOO_LEAD_FIELDS = {
    "name": {"string": "Opportunity", "type": "char"},
    "phone": {"string": "Phone", "type": "char"},
    "email_from": {"string": "Email", "type": "char"},
    "partner_id": {"string": "Customer", "type": "many2one", "relation": "res.partner"},
    "x_plastic_type": {"string": "Plastic Type", "type": "selection"},
}


def _odoo_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "odoo_url": ODOO_URL,
        "odoo_api_key": ODOO_API_KEY,
        "odoo_db": "test-db",
        **overrides,
    }
    return Settings(_env_file=None, **values)


@pytest.fixture
def odoo_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(converge_mod, "_domain_specs", {"plasticos": PLASTICOS_SPEC})
    monkeypatch.setattr(discover_mod, "get_settings", lambda: _odoo_settings())


def _mock_odoo_routes() -> dict[str, respx.Route]:
    def url(model: str, method: str) -> str:
        return f"{ODOO_URL}/json/2/{model}/{method}"

    return {
        "partner_fields": respx.post(url("res.partner", "fields_get")).mock(
            return_value=httpx.Response(200, json=ODOO_PARTNER_FIELDS)
        ),
        "partner_rows": respx.post(url("res.partner", "search_read")).mock(
            return_value=httpx.Response(
                200, json=[{"id": 1, "name": "Acme", "phone": "+1", "email": "a@a.test"}]
            )
        ),
        "lead_fields": respx.post(url("crm.lead", "fields_get")).mock(
            return_value=httpx.Response(200, json=ODOO_LEAD_FIELDS)
        ),
        "lead_rows": respx.post(url("crm.lead", "search_read")).mock(
            return_value=httpx.Response(200, json=[]),
        ),
    }


@respx.mock
def test_odoo_source_scan_works_end_to_end(client: TestClient, odoo_configured: None) -> None:
    """Acceptance: no fields[] supplied, both Odoo resources queried, one scanner, provenance kept."""
    routes = _mock_odoo_routes()

    resp = client.post(
        "/api/v1/scan",
        json={"domain": "plasticos", "tenant_id": "scrap-management", "source": "odoo"},
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    # both resources queried, exactly one fields_get + one search_read each, nothing else
    assert all(route.call_count == 1 for route in routes.values())
    assert len(respx.calls) == 4
    for call in respx.calls:
        assert call.request.headers["authorization"] == "bearer test-api-key"
        assert call.request.headers["x-odoo-database"] == "test-db"
        assert call.request.url.path.endswith(("/fields_get", "/search_read"))
    # live metadata → CRMField → existing scanner
    assert data["domain_id"] == "plasticos"
    assert data["total_crm_fields"] == 9
    matched = {(m["crm_field"], m["source_resource"]) for m in data["matched"]}
    assert ("phone", "res.partner") in matched
    assert ("phone", "crm.lead") in matched  # duplicate technical names survive
    assert ("x_materials_handled", "res.partner") in matched  # custom field discovered
    assert all(m["source_system"] == "odoo" for m in data["matched"])
    unmapped = {(u["crm_field"], u["source_resource"]) for u in data["unmapped"]}
    assert ("x_plastic_type", "crm.lead") in unmapped
    assert data["missing"] == []
    # secrets never leave the service
    assert ODOO_API_KEY not in resp.text


def test_manual_scan_still_works_without_source(client: TestClient, odoo_configured: None) -> None:
    resp = client.post(
        "/api/v1/scan",
        json={
            "domain": "plasticos",
            "tenant_id": "t",
            "fields": [{"name": "phone", "type": "char"}],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["matched_count"] == 1
    assert data["matched"][0]["source_system"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"domain": "plasticos", "tenant_id": "t"},
        {"domain": "plasticos", "tenant_id": "t", "source": "odoo", "fields": []},
    ],
    ids=["neither", "both"],
)
def test_requires_fields_xor_source(client: TestClient, odoo_configured: None, body: dict) -> None:
    resp = client.post("/api/v1/scan", json=body)
    assert resp.status_code == 422


def test_rejects_unknown_source(client: TestClient, odoo_configured: None) -> None:
    resp = client.post(
        "/api/v1/scan", json={"domain": "plasticos", "tenant_id": "t", "source": "salesforce"}
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("limit", [0, 101, 1000])
def test_rejects_invalid_sample_limit(
    client: TestClient, odoo_configured: None, limit: int
) -> None:
    resp = client.post(
        "/api/v1/scan",
        json={"domain": "plasticos", "tenant_id": "t", "source": "odoo", "sample_limit": limit},
    )
    assert resp.status_code == 422


@respx.mock
def test_sample_limit_forwarded_to_odoo(client: TestClient, odoo_configured: None) -> None:
    routes = _mock_odoo_routes()
    resp = client.post(
        "/api/v1/scan",
        json={"domain": "plasticos", "tenant_id": "t", "source": "odoo", "sample_limit": 3},
    )
    assert resp.status_code == 200
    body = httpx.Response(200, content=routes["lead_rows"].calls.last.request.content).json()
    assert body["limit"] == 3


def test_missing_odoo_config_is_controlled_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(converge_mod, "_domain_specs", {"plasticos": PLASTICOS_SPEC})
    monkeypatch.setattr(discover_mod, "get_settings", lambda: _odoo_settings(odoo_api_key=""))
    resp = client.post(
        "/api/v1/scan", json={"domain": "plasticos", "tenant_id": "t", "source": "odoo"}
    )
    assert resp.status_code == 503
    assert "ODOO_URL and ODOO_API_KEY" in resp.json()["detail"]


def test_missing_odoo_config_does_not_break_manual_scan(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(converge_mod, "_domain_specs", {"plasticos": PLASTICOS_SPEC})
    monkeypatch.setattr(
        discover_mod, "get_settings", lambda: _odoo_settings(odoo_url="", odoo_api_key="")
    )
    resp = client.post(
        "/api/v1/scan",
        json={
            "domain": "plasticos",
            "tenant_id": "t",
            "fields": [{"name": "name", "type": "char"}],
        },
    )
    assert resp.status_code == 200


@respx.mock
def test_odoo_failure_returns_controlled_upstream_error(
    client: TestClient, odoo_configured: None
) -> None:
    _mock_odoo_routes()
    respx.post(f"{ODOO_URL}/json/2/crm.lead/fields_get").mock(
        return_value=httpx.Response(403, json={"name": "odoo.exceptions.AccessError"})
    )
    resp = client.post(
        "/api/v1/scan", json={"domain": "plasticos", "tenant_id": "t", "source": "odoo"}
    )
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "crm.lead" in detail and "status=403" in detail
    assert ODOO_API_KEY not in resp.text
    # Contacts-only success must not masquerade as a full CRM scan
    assert "matched" not in resp.json()


@respx.mock
def test_odoo_unavailable_returns_502_not_empty_success(
    client: TestClient, odoo_configured: None
) -> None:
    respx.post(f"{ODOO_URL}/json/2/res.partner/fields_get").mock(
        side_effect=httpx.ConnectError("refused")
    )
    resp = client.post(
        "/api/v1/scan", json={"domain": "plasticos", "tenant_id": "t", "source": "odoo"}
    )
    assert resp.status_code == 502
    assert "res.partner" in resp.json()["detail"]
