"""
tests/test_pr22_converge_endpoint.py

Proves GAP-3 and GAP-4:
  GAP-3: converge.configure() is called before any request (503 otherwise)
  GAP-4: POST /v1/converge actually calls convergence_controller.run_convergence_loop()
         and returns real pass/field counts, not stub zeros
"""

from __future__ import annotations

import os
from unittest.mock import patch

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


# ── Route harness for WU-05 / WU-06 ───────────────────────────────────────


async def _post_converge(body: dict, run_loop, patches: dict | None = None):
    """POST /v1/converge with run_convergence_loop replaced; restores module wiring."""
    from contextlib import ExitStack

    from httpx import ASGITransport, AsyncClient

    import app.api.v1.converge as cm
    from app.engines.convergence.loop_state import LoopStateStore
    from app.main import app
    from app.services.enrichment_profile import ProfileRegistry

    class _Store(LoopStateStore):
        def __init__(self) -> None:
            self._states: dict = {}

        async def save(self, state):
            self._states[state.run_id] = state

        async def load(self, run_id):
            return self._states.get(run_id)

        async def list_active(self, domain=None):
            return []

    names = ("_state_store", "_profile_registry", "_domain_specs", "_kb_resolver", "_idem_store")
    original = {n: getattr(cm, n) for n in names}
    cm.configure(
        state_store=_Store(),
        profile_registry=ProfileRegistry(),
        domain_specs={},
        kb_resolver=None,
        idem_store=None,
    )
    try:
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "app.engines.convergence_controller.run_convergence_loop", side_effect=run_loop
                )
            )
            for target, value in (patches or {}).items():
                stack.enter_context(patch(target, value))
            async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
                return await c.post("/v1/converge", json=body, headers=_auth_headers())
    finally:
        for n, v in original.items():
            setattr(cm, n, v)


@pytest.mark.asyncio
async def test_converge_reports_the_loops_stop_reason():
    """WU-05 / F-018: convergence_reason is the loop's reason, not a constant."""

    async def run_loop(**kw):
        response = _make_conv_response()
        response.convergence_reason = "budget_exhausted"
        return response

    resp = await _post_converge({"entity": {"id": "e1"}, "max_passes": 3}, run_loop)
    assert resp.status_code == 200, resp.text
    assert resp.json()["convergence_reason"] == "budget_exhausted"


# ── WU-06 / F-007 / D-10: faithful request mapping ────────────────────────


def _capturing_loop(calls: list, response=None):
    async def run_loop(**kw):
        calls.append(kw)
        return response or _make_conv_response()

    return run_loop


@pytest.mark.asyncio
async def test_request_budget_and_deltas_map_faithfully():
    calls: list = []
    resp = await _post_converge(
        {"entity": {"id": "e1"}, "max_passes": 5, "max_budget_tokens": 2000},
        _capturing_loop(calls),
    )
    assert resp.status_code == 200, resp.text
    cfg = calls[0]["convergence_config"]
    assert cfg.max_passes == 5
    assert cfg.max_budget_tokens == 2000
    # min_improvement_delta / confidence_threshold keep their own defaults;
    # convergence_threshold (2.0) is the uncertainty boundary only.
    assert cfg.min_improvement_delta == 0.05
    assert cfg.confidence_threshold == 0.85
    assert cfg.uncertainty_threshold == 2.0


@pytest.mark.asyncio
async def test_improving_confidence_runs_more_than_two_passes_through_the_route():
    from app.engines.convergence_controller import run_convergence_loop as real_loop
    from app.models.schemas import EnrichResponse

    script = [0.3, 0.5, 0.7, 0.9]
    calls = {"n": 0}

    async def enricher(*args, **kwargs):
        conf = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        return EnrichResponse(
            fields={"industry": f"v{conf}"},
            confidence=conf,
            tokens_used=10,
            feature_vector={"per_field_confidence": {"industry": conf}},
        )

    async def run_loop(**kw):
        return await real_loop(**kw, enricher=enricher)

    resp = await _post_converge({"entity": {"id": "e1"}, "max_passes": 5}, run_loop)
    assert resp.status_code == 200, resp.text
    assert resp.json()["passes_completed"] > 2


@pytest.mark.asyncio
async def test_domain_loads_only_rules_and_hints_through_the_sdk_loader():
    from unittest.mock import AsyncMock

    loader = AsyncMock(return_value=({"priority_fields": ["website"]}, [{"rule": "r1"}]))
    calls: list = []
    resp = await _post_converge(
        {"entity": {"id": "e1"}, "domain": "plasticos"},
        _capturing_loop(calls),
        patches={"app.engines.handlers._load_domain_convergence_context": loader},
    )
    assert resp.status_code == 200, resp.text
    loader.assert_awaited_once_with(domain_id="plasticos", node_label=None)
    assert calls[0]["inference_rules"] == [{"rule": "r1"}]
    assert calls[0]["domain_hints"] == {"priority_fields": ["website"]}
    # D-10: no full domain_spec on the direct HTTP path. The route reached the
    # patched handlers._load_domain_convergence_context — the same loader the
    # SDK canonical converge calls — keyed by the same tenant/domain id.
    assert "domain_spec" not in calls[0]


@pytest.mark.asyncio
async def test_illegal_tenant_domain_is_rejected_before_any_read():
    from unittest.mock import AsyncMock

    loader = AsyncMock(return_value=({}, []))
    calls: list = []
    resp = await _post_converge(
        {"entity": {"id": "e1"}, "domain": "Not A Tenant"},
        _capturing_loop(calls),
        patches={"app.engines.handlers._load_domain_convergence_context": loader},
    )
    assert resp.status_code == 400
    assert loader.await_count == 0
    assert calls == []


@pytest.mark.asyncio
async def test_domain_read_takes_the_direct_tenant_posture():
    from fastapi import HTTPException

    def refuse():
        raise HTTPException(status_code=403, detail="gate ingress required")

    calls: list = []
    resp = await _post_converge(
        {"entity": {"id": "e1"}, "domain": "plasticos"},
        _capturing_loop(calls),
        patches={"app.api.v1.converge.ensure_direct_tenant_read_allowed": refuse},
    )
    assert resp.status_code == 403
    assert calls == []


@pytest.mark.asyncio
async def test_human_approval_mode_holds_the_run_for_approval():
    resp = await _post_converge(
        {"entity": {"id": "e1"}, "approval_mode": "human"}, _capturing_loop([])
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "human_hold"


@pytest.mark.asyncio
async def test_auto_approval_mode_converges():
    resp = await _post_converge({"entity": {"id": "e1"}}, _capturing_loop([]))
    assert resp.json()["status"] == "converged"


@pytest.mark.asyncio
async def test_unknown_approval_mode_is_rejected():
    calls: list = []
    resp = await _post_converge(
        {"entity": {"id": "e1"}, "approval_mode": "yolo"}, _capturing_loop(calls)
    )
    assert resp.status_code == 422
    assert calls == []
