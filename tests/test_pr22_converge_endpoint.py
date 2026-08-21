"""
tests/test_pr22_converge_endpoint.py

Proves GAP-3 and GAP-4:
  GAP-3: converge.configure() is called before any request (503 otherwise)
  GAP-4: POST /v1/converge actually calls convergence_controller.run_convergence_loop()
         and returns real pass/field counts, not stub zeros
"""

from __future__ import annotations

import hashlib
import os
from unittest.mock import patch

import pytest

from app.core.config import get_settings

_TEST_API_KEY = "pass"
os.environ["API_KEY_HASH"] = hashlib.sha256(_TEST_API_KEY.encode()).hexdigest()
get_settings.cache_clear()
AUTH = {"X-API-Key": _TEST_API_KEY}


def _make_conv_response(
    fields: dict | None = None,
    pass_count: int = 2,
    tokens_used: int = 800,
    state: str = "completed",
):
    from app.models.schemas import EnrichResponse

    return EnrichResponse(
        fields=fields or {"material_type": "HDPE", "facility_tier": "tier-2"},
        confidence=0.88,
        state=state,
        tokens_used=tokens_used,
        processing_time_ms=1600,
        pass_count=pass_count,
    )


def _auth_headers() -> dict[str, str]:
    return dict(AUTH)


@pytest.mark.asyncio
async def test_converge_503_when_not_configured():
    """GAP-3: /v1/converge must return 503 if configure() was never called."""

    from httpx import ASGITransport, AsyncClient

    import app.api.v1.converge as cm
    from app.main import app

    original_store = cm._state_store
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            cm._state_store = None
            resp = await client.post(
                "/v1/converge",
                json={
                    "entity": {"id": "ent-001", "Name": "Acme"},
                    "object_type": "Account",
                    "objective": "test",
                },
                headers=_auth_headers(),
            )
        assert resp.status_code == 503
    finally:
        cm._state_store = original_store


@pytest.mark.asyncio
async def test_converge_calls_run_convergence_loop():
    """GAP-4: POST /v1/converge must delegate to convergence_controller.run_convergence_loop."""
    from app.engines.convergence.loop_state import LoopStateStore
    from app.services.enrichment_profile import ProfileRegistry

    run_loop_calls: list = []

    async def mock_run_loop(
        request, settings, kb_resolver, idem_store, convergence_config=None, **kw
    ):
        run_loop_calls.append(
            {
                "entity": request.entity,
                "passes": convergence_config.max_passes if convergence_config else 5,
            }
        )
        return _make_conv_response()

    class MockStateStore(LoopStateStore):
        _states: dict = {}

        async def save(self, state):
            self._states[state.run_id] = state

        async def load(self, run_id):
            return self._states.get(run_id)

        async def list_active(self, domain=None):
            return []

    import app.api.v1.converge as cm

    cm.configure(
        state_store=MockStateStore(),
        profile_registry=ProfileRegistry(),
        domain_specs={},
        kb_resolver=None,
        idem_store=None,
    )

    with patch(
        "app.engines.convergence_controller.run_convergence_loop",
        side_effect=mock_run_loop,
    ):
        from httpx import ASGITransport, AsyncClient

        from app.main import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            cm.configure(
                state_store=MockStateStore(),
                profile_registry=ProfileRegistry(),
                domain_specs={},
                kb_resolver=None,
                idem_store=None,
            )
            resp = await client.post(
                "/v1/converge",
                json={
                    "entity": {"id": "ent-001", "Name": "Acme Plastics"},
                    "object_type": "Account",
                    "objective": "Full enrichment",
                    "max_passes": 3,
                },
                headers=_auth_headers(),
            )

    assert resp.status_code == 200
    body = resp.json()
    assert len(run_loop_calls) == 1, "run_convergence_loop must be called exactly once"
    assert run_loop_calls[0]["passes"] == 3
    assert body["passes_completed"] == 2
    assert body["tokens_used"] == 800
    assert body["fields_discovered"] == 2
    assert body["status"] == "converged"


@pytest.mark.asyncio
async def test_converge_batch_processes_inline_entities():
    """GAP-9: converge/batch must process inline entity list and return real counts."""
    from app.engines.convergence.loop_state import LoopStateStore
    from app.services.enrichment_profile import EnrichmentProfile, ProfileRegistry

    processed: list = []

    async def mock_run_loop(
        request, settings, kb_resolver, idem_store, convergence_config=None, **kw
    ):
        processed.append(request.entity.get("id"))
        return _make_conv_response(fields={"material_type": "PP"}, tokens_used=400)

    class MockStateStore(LoopStateStore):
        _states: dict = {}

        async def save(self, state):
            self._states[state.run_id] = state

        async def load(self, run_id):
            return self._states.get(run_id)

        async def list_active(self, domain=None):
            return []

    from httpx import ASGITransport, AsyncClient

    import app.api.v1.converge as cm
    from app.main import app

    profile = EnrichmentProfile(profile_name="test_batch", batch_size=10, max_passes=2)
    registry = ProfileRegistry()
    registry.register(profile)

    with patch(
        "app.engines.convergence_controller.run_convergence_loop",
        side_effect=mock_run_loop,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            cm.configure(
                state_store=MockStateStore(),
                profile_registry=registry,
                domain_specs={},
            )
            resp = await client.post(
                "/v1/converge/batch",
                json={
                    "profile_name": "test_batch",
                    "entities": [
                        {"id": "e1", "object_type": "Account"},
                        {"id": "e2", "object_type": "Account"},
                    ],
                },
                headers=_auth_headers(),
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["entities_selected"] == 2
    assert body["entities_processed"] == 2
    assert body["total_tokens"] == 800
    assert len(body["run_ids"]) == 2
    assert sorted(processed) == ["e1", "e2"]
