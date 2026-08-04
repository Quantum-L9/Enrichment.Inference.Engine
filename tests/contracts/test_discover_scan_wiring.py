"""Route-to-engine wiring contract tests for the schema-discovery API.

Pins two pre-existing functional bugs fixed narrowly:

Bug 1 — /api/v1/discover imported a non-existent ``discover`` symbol from
``app.engines.schema_discovery`` and always 500'd. The route now delegates to
the canonical ``handle_discover`` action handler.

Bug 2 — /api/v1/scan awaited ``scan_crm_fields`` with the wrong keyword
arguments (``domain=``, ``tenant_id=``, ``settings=``). The canonical function
is synchronous with signature ``scan_crm_fields(crm_fields, domain_spec)``. The
route now resolves the domain spec and calls it correctly.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import app.api.v1.discover as discover_mod
from app.services.crm_field_scanner import CRMField, ScanResult


@pytest_asyncio.fixture
async def client():
    """FastAPI client with API-key auth overridden (auth is not under test)."""
    from app.core.auth import verify_api_key
    from app.main import app

    saved_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[verify_api_key] = lambda: "test-principal"
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="https://testserver") as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved_overrides)


# ── Bug 1: /api/v1/discover ─────────────────────────────────────────────


def test_discover_route_wires_to_canonical_handler():
    """The route binds the canonical handler, not a missing ``discover`` symbol."""
    from app.engines.handlers import handle_discover as canonical

    assert discover_mod.handle_discover is canonical
    # The old broken import target must remain absent (documents the root cause).
    import app.engines.schema_discovery as sd

    assert not hasattr(sd, "discover")


@pytest.mark.asyncio
async def test_discover_invokes_handle_discover_with_mapped_payload(client, monkeypatch):
    sentinel = {"enrichment": {"ok": True}, "schema_proposal": {"stage": "discovered"}}
    mock = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(discover_mod, "handle_discover", mock)

    resp = await client.post(
        "/api/v1/discover",
        json={
            "entity_id": "acct-42",
            "domain": "plasticos",
            "object_type": "Account",
            "tenant_id": "tenant-7",
        },
    )

    assert resp.status_code == 200
    assert resp.json() == sentinel
    mock.assert_awaited_once()
    kwargs = mock.await_args.kwargs
    assert kwargs["tenant"] == "tenant-7"
    payload = kwargs["payload"]
    assert payload["entity"] == {"id": "acct-42"}
    assert payload["object_type"] == "Account"
    assert payload["kb_context"] == "plasticos"


# ── Bug 2: /api/v1/scan ─────────────────────────────────────────────────


def _stub_scan_result() -> ScanResult:
    return ScanResult(
        domain_id="plasticos",
        domain_version="0.3.0",
        total_crm_fields=1,
        total_domain_properties=1,
    )


@pytest.mark.asyncio
async def test_scan_calls_sync_scanner_with_positional_args(client, monkeypatch):
    captured: dict[str, Any] = {}
    stub_spec = {"ontology": {"nodes": []}}
    monkeypatch.setattr(discover_mod, "_resolve_domain_spec", lambda domain, settings: stub_spec)

    # A strictly 2-positional, synchronous stub: reintroducing the old
    # domain=/tenant_id=/settings= kwargs or an `await` would raise here.
    def fake_scan(crm_fields, domain_spec):
        captured["crm_fields"] = crm_fields
        captured["domain_spec"] = domain_spec
        return _stub_scan_result()

    monkeypatch.setattr(discover_mod, "run_crm_field_scan", fake_scan)

    resp = await client.post(
        "/api/v1/scan",
        json={
            "fields": [{"name": "material_grade", "type": "string"}],
            "domain": "plasticos",
            "tenant_id": "tenant-7",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["domain_id"] == "plasticos"
    # crm_fields converted to CRMField objects, domain_spec passed positionally.
    assert captured["domain_spec"] is stub_spec
    assert [type(f) for f in captured["crm_fields"]] == [CRMField]
    assert captured["crm_fields"][0].name == "material_grade"


@pytest.mark.asyncio
async def test_scan_returns_scan_result_for_known_domain(client):
    """End-to-end against the real plasticos domain spec — no mocks."""
    discover_mod._DOMAIN_SPEC_CACHE.pop("plasticos", None)
    resp = await client.post(
        "/api/v1/scan",
        json={
            "fields": [
                {"name": "material_grade", "type": "string"},
                {"name": "city", "type": "string"},
            ],
            "domain": "plasticos",
            "tenant_id": "tenant-7",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert {"matched", "unmapped", "missing", "coverage_ratio", "domain_id"}.issubset(body)
    # material_grade and city are real Facility properties → both matched.
    assert body["matched_count"] >= 2
    matched_props = {m["domain_property"] for m in body["matched"]}
    assert {"material_grade", "city"}.issubset(matched_props)


@pytest.mark.asyncio
async def test_scan_unknown_domain_returns_404(client):
    resp = await client.post(
        "/api/v1/scan",
        json={
            "fields": [{"name": "material_grade", "type": "string"}],
            "domain": "no-such-domain-xyz",
            "tenant_id": "tenant-7",
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_scan_rejects_domain_path_traversal(client):
    resp = await client.post(
        "/api/v1/scan",
        json={
            "fields": [{"name": "material_grade", "type": "string"}],
            "domain": "../../etc/passwd",
            "tenant_id": "tenant-7",
        },
    )
    assert resp.status_code == 404
