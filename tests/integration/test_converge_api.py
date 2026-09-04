"""
Integration tests for the /v1/converge endpoint.
Uses mocked LLM calls to avoid external API costs in CI.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import get_settings

_TEST_API_KEY = "pass"
# Precomputed sha256(_TEST_API_KEY) — the same literal digest already used by
# tests/test_api.py, tests/test_rate_limiter.py and tests/services/
# test_gate_registration.py. Stored rather than computed so no hashing call
# exists here at all: hashing a fixture key inline is what CodeQL flags as
# py/weak-sensitive-data-hashing (SHA-256 is not a computationally expensive
# password hash), and usedforsecurity=False does not suppress that query.
# app/core/auth.py still does the real comparison via hmac.compare_digest.
os.environ["API_KEY_HASH"] = "d74ff0ee8da3b9806b18c877dbf29bbde50b5bd8e4dad7a3a725000feb82e8f1"
get_settings.cache_clear()
AUTH = {"X-API-Key": _TEST_API_KEY}


@pytest.mark.asyncio
async def test_converge_health(api_client):
    """Health endpoint returns 200."""
    resp = await api_client.get("/api/v1/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
# enrichment_orchestrator does `from ..services.perplexity_client import
# query_perplexity`, so the alias is already bound by the time this test runs
# (the api_client fixture imports app.main). Patching the *source* module would
# leave that binding untouched and let the call reach the live Sonar API —
# patch the name the orchestrator actually calls.
@patch("app.engines.enrichment_orchestrator.query_perplexity", new_callable=AsyncMock)
async def test_converge_single_entity(mock_llm, api_client):
    """POST /v1/converge converges and returns the ConvergeSingleResponse shape."""
    import app.api.v1.converge as converge_module
    from app.engines.convergence.loop_state import LoopState, LoopStateStore
    from app.services.enrichment_profile import ProfileRegistry
    from app.services.kb_resolver import KBResolver
    from app.services.perplexity_client import SonarResponse

    # The pass schema is built from the planner's priority fields, which with
    # no domain spec are the entity's own keys; a provider answer that omits
    # them is dropped by schema validation. Before the consensus engine
    # stopped treating the `confidence` metadata key as an enriched field,
    # this test passed on that leaked key alone.
    mock_llm.return_value = SonarResponse(
        data={
            "id": "test-001",
            "company_name": "Alpha Recyclers Inc.",
            "materials_handled": ["HDPE", "PP"],
            "material_grade": "A",
            "contamination_tolerance_pct": 0.02,
            "facility_tier": "tier_1",
            "confidence": 0.9,
        },
        tokens_used=1200,
        citations=["https://example.com/alpha-recyclers"],
        model="sonar",
    )

    class _MemoryStateStore(LoopStateStore):
        """Minimal in-process store so the endpoint is configured (GAP-3)."""

        def __init__(self) -> None:
            self._states: dict[str, LoopState] = {}

        async def save(self, state: LoopState) -> None:
            self._states[state.run_id] = state

        async def load(self, run_id: str) -> LoopState | None:
            return self._states.get(run_id)

        async def list_active(self, domain: str | None = None) -> list[LoopState]:
            return list(self._states.values())

    payload = {
        "entity": {
            "id": "test-001",
            "company_name": "Alpha Recyclers",
            "materials_handled": ["HDPE"],
        },
        "object_type": "Account",
        "domain": "plasticos",
        "objective": "Full entity enrichment",
        "max_passes": 2,
        "max_budget_tokens": 5000,
    }

    # configure() writes five module-level globals; snapshot and restore all of
    # them so this test cannot leak wiring into whatever runs next.
    _configured = (
        "_state_store",
        "_profile_registry",
        "_domain_specs",
        "_kb_resolver",
        "_idem_store",
    )
    original = {name: getattr(converge_module, name) for name in _configured}
    converge_module.configure(
        state_store=_MemoryStateStore(),
        profile_registry=ProfileRegistry(),
        domain_specs={},
        # Mirrors app.main's wiring. KBResolver tolerates a missing kb_dir
        # (logs a warning, resolves to empty), so this stays hermetic.
        kb_resolver=KBResolver(get_settings().kb_dir),
        idem_store=None,
    )
    try:
        resp = await api_client.post("/v1/converge", json=payload, headers=AUTH)
    finally:
        for name, value in original.items():
            setattr(converge_module, name, value)

    # Configured store + mocked LLM: a 503 or 422 here is a real regression,
    # not an accepted outcome.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "run_id",
        "status",
        "passes_completed",
        "fields_discovered",
        "tokens_used",
        "cost_usd",
        "convergence_reason",
    }
    assert body["run_id"]
    assert body["status"] == "converged"
    assert body["passes_completed"] >= 1
    assert body["fields_discovered"] >= 1
    assert body["tokens_used"] > 0
    assert mock_llm.await_count >= 1


@pytest.mark.asyncio
async def test_scan_endpoint_rejects_unknown_domain_without_auth(api_client):
    """Auth runs first — unauthenticated POST /v1/scan is 401."""
    resp = await api_client.post(
        "/v1/scan",
        json={
            "fields": [{"name": "company_name", "field_type": "string"}],
            "domain": "nonexistent-domain-xyz",
        },
        headers={"X-API-Key": ""},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_scan_endpoint_rejects_unknown_domain(api_client):
    """Authenticated POST /v1/scan returns the live unknown-domain status (400)."""
    resp = await api_client.post(
        "/v1/scan",
        json={
            "fields": [{"name": "company_name", "field_type": "string"}],
            "domain": "nonexistent-domain-xyz",
        },
        headers=AUTH,
    )
    assert resp.status_code == 400
