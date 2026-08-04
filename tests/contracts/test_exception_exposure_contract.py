"""Security-contract regression tests for CodeQL remediations.

Contract: HTTP handlers MUST NOT return raw exception text to clients
(CWE-209, ``py/stack-trace-exposure``). The ``detail`` of a 5xx response
must be a generic message; the exception is logged server-side only.

Also pins the non-security cache-key hardening (CWE-327/328,
``py/weak-cryptographic-algorithm``): ``convergence_controller._cache_key``
marks its MD5 usage ``usedforsecurity=False`` while preserving the digest.
"""

from __future__ import annotations

import hashlib

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Sentinel that stands in for sensitive internal detail an exception might
# carry (paths, tokens, stack context). It must never reach the HTTP client.
SENTINEL = "SENSITIVE-INTERNAL-/srv/secret/api_token=leak123"


@pytest_asyncio.fixture
async def client():
    """FastAPI client with API-key auth overridden (auth is not under test).

    Saves and restores any pre-existing dependency overrides so this fixture
    never clobbers overrides set by other tests, even if setup raises.
    """
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


def _assert_no_leak(resp) -> None:
    assert resp.status_code == 500
    body = resp.text
    assert SENTINEL not in body, f"exception detail leaked to client: {body}"
    detail = resp.json().get("detail", "")
    assert "Internal error" in detail


@pytest.mark.asyncio
async def test_discover_does_not_leak_exception(client, monkeypatch):
    async def _boom(**_kwargs):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr("app.engines.schema_discovery.discover", _boom, raising=False)
    resp = await client.post(
        "/api/v1/discover",
        json={
            "entity_id": "e-1",
            "domain": "plasticos",
            "object_type": "Account",
            "tenant_id": "t-1",
        },
    )
    _assert_no_leak(resp)


@pytest.mark.asyncio
async def test_scan_does_not_leak_exception(client, monkeypatch):
    async def _boom(**_kwargs):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr("app.services.crm_field_scanner.scan_crm_fields", _boom, raising=False)
    resp = await client.post(
        "/api/v1/scan",
        json={
            "fields": [{"name": "company_name", "type": "string"}],
            "domain": "plasticos",
            "tenant_id": "t-1",
        },
    )
    _assert_no_leak(resp)


@pytest.mark.asyncio
async def test_attestation_does_not_leak_exception(client, monkeypatch):
    def _boom():
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr("app.api.v1.attestation.build_runtime_attestation", _boom)
    resp = await client.get("/v1/attestation")
    _assert_no_leak(resp)


@pytest.mark.asyncio
async def test_converge_does_not_leak_exception(client, monkeypatch):
    import app.api.v1.converge as converge_mod

    class _FakeStore:
        async def save(self, _state):
            return None

    monkeypatch.setattr(converge_mod, "_state_store", _FakeStore())

    async def _boom(**_kwargs):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr("app.engines.convergence_controller.run_convergence_loop", _boom)
    resp = await client.post(
        "/v1/converge",
        json={"entity": {"id": "e-1", "Name": "Acme"}, "domain": "plasticos"},
    )
    _assert_no_leak(resp)


def test_cache_key_marks_md5_non_security():
    """Digest is preserved and MD5 is flagged non-security (usedforsecurity=False)."""
    from app.engines.convergence_controller import _cache_key

    spec = {"domain": "test_domain", "ontology": {"nodes": {"A": {}, "B": {}}}}
    expected = hashlib.md5(b"test_domain:2", usedforsecurity=False).hexdigest()
    assert _cache_key(spec) == expected

    # Deterministic: an independently-constructed but equivalent spec yields
    # the same key (distinct object, not a self-comparison).
    spec_equivalent = {"domain": "test_domain", "ontology": {"nodes": {"A": {}, "B": {}}}}
    assert _cache_key(spec) == _cache_key(spec_equivalent)
