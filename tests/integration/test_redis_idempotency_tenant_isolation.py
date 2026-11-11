"""EIE-FIX-001 — the Redis enrichment replay cache is tenant-isolated.

`IdempotencyStore` keyed Redis on `enrich:idem:{idempotency_key}` and nothing
else, and `enrich_entity` returned a hit without running enrichment. An
idempotency key is caller-chosen, so two tenants using the same string is
ordinary: the second tenant was answered with the first tenant's enrichment.
The same key reused inside one tenant for a different request was answered
with the earlier request's result.

These tests drive the real entrypoints — the SDK `enrich` / `enrichbatch`
handlers and the HTTP `/api/v1/enrich` route — against a real Redis, with only
the provider and the post-enrich side effects replaced. Postgres idempotency was
already made per-tenant (migration 002, tests/unit/test_tenant_scoped_idempotency.py);
this is the Redis replay layer in front of it.

Redis comes from REDIS_URL, as the CI `test` job provides it (pr-pipeline.yml,
`redis:7-alpine` service). Each test uses fresh random tenants and keys and a
short TTL, so nothing is flushed and parallel workers cannot collide. Where no
Redis answers, the module skips (fails under EIE_REQUIRE_INTEGRATION=1, as in
CI's `integration` job): a deterministic substitute is exercised by
tests/unit/test_idempotency_store.py, so the skip hides no coverage of the
store's own logic.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
import redis.asyncio as redis_async

from app.engines import handlers
from app.services.idempotency import IdempotencyStore
from app.services.perplexity_client import SonarResponse
from tests.conftest import skip_or_fail_unavailable

pytestmark = [pytest.mark.integration]

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
FIXTURES = Path(__file__).resolve().parents[1] / "contracts" / "fixtures"


def _fixture(name: str, key: str) -> dict[str, Any]:
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    payload["idempotency_key"] = key
    return payload


class Provider:
    """Stands in for the Sonar call. Every variation answers with `marker`.

    One request fans out to several variations, so the tests reason about which
    markers were ever served — never about raw call counts.
    """

    def __init__(self) -> None:
        self.marker = "unset"
        self.calls: list[str] = []

    async def __call__(self, **_: Any) -> SonarResponse:
        self.calls.append(self.marker)
        return SonarResponse(
            data={"confidence": 0.9, "polymer_type": "HDPE", "material_grade": self.marker},
            tokens_used=10,
            citations=[],
            model="sonar",
        )

    def served(self, marker: str) -> bool:
        return marker in self.calls


def _owner(result: dict[str, Any]) -> Any:
    return (result.get("fields") or {}).get("material_grade")


@pytest_asyncio.fixture
async def store():
    probe = redis_async.from_url(REDIS_URL)
    try:
        await probe.ping()
    except Exception as exc:  # noqa: BLE001 — any failure means no usable Redis
        skip_or_fail_unavailable(f"no Redis at REDIS_URL ({type(exc).__name__}); CI provides one")
    finally:
        await probe.aclose()

    real = IdempotencyStore(REDIS_URL, ttl=60)
    yield real
    await real.close()


@pytest.fixture
def settings() -> MagicMock:
    s = MagicMock()
    s.perplexity_api_key = "test-key"
    s.perplexity_model = "sonar"
    s.max_concurrent_variations = 1
    s.default_timeout_seconds = 30
    s.l9_enrichment_provider = "perplexity"
    return s


@pytest.fixture
def kb() -> MagicMock:
    resolver = MagicMock()
    resolver.resolve = MagicMock(
        return_value={
            "context_text": "",
            "content_hash": "kb0",
            "fragment_ids": [],
            "kb_files": [],
        }
    )
    return resolver


@pytest.fixture
def wired(store, settings, kb, monkeypatch):
    """The SDK handler module, bound to the real store, provider replaced."""
    monkeypatch.setattr(handlers, "_idem", store)
    monkeypatch.setattr(handlers, "_kb", kb)
    monkeypatch.setattr(handlers, "get_settings", lambda: settings)
    monkeypatch.setattr(handlers, "_persist_and_sync", AsyncMock())
    provider = Provider()
    with patch("app.engines.enrichment_orchestrator.query_perplexity", provider):
        yield provider


def _tenants() -> tuple[str, str]:
    run = uuid.uuid4().hex[:8]
    return f"acme-{run}", f"globex-{run}"


def _key() -> str:
    return f"eie-fix-001-{uuid.uuid4()}"


async def test_a_second_tenant_is_not_answered_from_the_first_tenants_cache(wired):
    acme, globex = _tenants()
    key = _key()
    payload = _fixture("enrich_request_same_key_variant_a.json", key)

    wired.marker = "acme-only"
    first = await handlers.handle_enrich(acme, payload)
    wired.marker = "globex-only"
    second = await handlers.handle_enrich(globex, dict(payload))

    assert first["state"] == "completed"
    assert _owner(first) == "acme-only"
    assert wired.served("globex-only"), "globex's request must run, not replay acme's"
    assert _owner(second) == "globex-only", f"globex received acme's result: {second['fields']!r}"


async def test_an_identical_retry_in_the_same_tenant_replays_without_a_provider_call(wired):
    acme, _ = _tenants()
    payload = _fixture("enrich_request_same_key_variant_a.json", _key())

    wired.marker = "acme-only"
    first = await handlers.handle_enrich(acme, payload)
    wired.marker = "must-not-be-called"
    retry = await handlers.handle_enrich(acme, dict(payload))

    assert first["state"] == "completed"
    assert not wired.served("must-not-be-called"), "a legitimate retry reached the provider"
    assert retry == first


async def test_a_key_reused_for_a_different_request_in_one_tenant_is_refused(wired):
    acme, _ = _tenants()
    key = _key()

    wired.marker = "request-a"
    first = await handlers.handle_enrich(
        acme, _fixture("enrich_request_same_key_variant_a.json", key)
    )
    wired.marker = "must-not-be-called"
    reused = await handlers.handle_enrich(
        acme, _fixture("enrich_request_same_key_variant_b.json", key)
    )

    assert _owner(first) == "request-a"
    assert reused["state"] == "failed", (
        f"request B was answered with request A's result: {reused['fields']!r}"
    )
    assert (reused["failure_reason"] or "").startswith("idempotency_key_conflict")
    assert not reused["fields"]
    assert not wired.served("must-not-be-called"), "a refused reuse reached the provider"
    handlers._persist_and_sync.assert_awaited_once()  # only request A persisted


async def test_the_refusal_does_not_overwrite_the_original_binding(wired):
    acme, _ = _tenants()
    key = _key()
    payload_a = _fixture("enrich_request_same_key_variant_a.json", key)

    wired.marker = "request-a"
    first = await handlers.handle_enrich(acme, payload_a)
    wired.marker = "must-not-be-called"
    await handlers.handle_enrich(acme, _fixture("enrich_request_same_key_variant_b.json", key))
    retry_a = await handlers.handle_enrich(acme, dict(payload_a))

    assert first["state"] == "completed"
    assert retry_a == first
    assert not wired.served("must-not-be-called")


async def test_batch_entities_are_scoped_to_the_calling_tenant(wired):
    acme, globex = _tenants()
    entity = _fixture("enrich_request_same_key_variant_a.json", _key())

    wired.marker = "acme-only"
    acme_batch = await handlers.handle_enrichbatch(acme, {"entities": [entity]})
    wired.marker = "globex-only"
    globex_batch = await handlers.handle_enrichbatch(globex, {"entities": [dict(entity)]})

    assert _owner(acme_batch["results"][0]) == "acme-only"
    assert _owner(globex_batch["results"][0]) == "globex-only"


async def test_sdk_tenants_never_share_a_binding_with_the_http_surface(wired, store, settings, kb):
    """`/api/v1/enrich` has no tenant; its principal is the authenticated API key.

    A Gate-routed tenant and the HTTP principal using one key must each run.
    The HTTP leg runs in this event loop (ASGITransport) because the Redis pool
    is bound to it.
    """
    from httpx import ASGITransport, AsyncClient

    from app import main
    from app.core.auth import verify_api_key
    from app.core.config import get_settings

    acme, _ = _tenants()
    payload = _fixture("enrich_request_same_key_variant_a.json", _key())

    wired.marker = "acme-only"
    sdk = await handlers.handle_enrich(acme, payload)

    wired.marker = "http-only"
    with patch.object(main, "_idem", store), patch.object(main, "_kb", kb):
        main.app.dependency_overrides[verify_api_key] = lambda: "http-principal-key"
        main.app.dependency_overrides[get_settings] = lambda: settings
        try:
            async with AsyncClient(
                transport=ASGITransport(app=main.app), base_url="https://testserver"
            ) as client:
                response = await client.post("/api/v1/enrich", json=dict(payload))
        finally:
            main.app.dependency_overrides.pop(verify_api_key, None)
            main.app.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 200
    assert _owner(sdk) == "acme-only"
    assert _owner(response.json()) == "http-only", "the HTTP caller received a tenant's result"


class SlowProvider:
    """Every request gets a distinct answer, and each call yields to the loop,
    so two requests started together are genuinely in flight at once."""

    def __init__(self) -> None:
        self._seq = itertools.count()

    async def __call__(self, **_: Any) -> SonarResponse:
        marker = f"run-{next(self._seq)}"
        await asyncio.sleep(0.05)
        return SonarResponse(
            data={"confidence": 0.9, "polymer_type": "HDPE", "material_grade": marker},
            tokens_used=10,
            citations=[],
            model="sonar",
        )


def _single_variation(name: str, key: str) -> dict[str, Any]:
    payload = _fixture(name, key)
    payload["max_variations"] = 1  # one provider call per request: one marker per run
    return payload


@pytest.fixture
def concurrent(wired):
    with patch("app.engines.enrichment_orchestrator.query_perplexity", SlowProvider()):
        yield


async def test_a_retry_in_flight_with_the_original_gets_the_bound_result(concurrent):
    """Salesforce/Odoo retry on timeout while the original is still running."""
    acme, _ = _tenants()
    payload = _single_variation("enrich_request_same_key_variant_a.json", _key())

    original, retry = await asyncio.gather(
        handlers.handle_enrich(acme, payload), handlers.handle_enrich(acme, dict(payload))
    )
    later = await handlers.handle_enrich(acme, dict(payload))

    assert original["state"] == retry["state"] == "completed"
    assert _owner(original) == _owner(retry) == _owner(later), (
        "callers of one logical request were given different results"
    )


async def test_a_different_request_in_flight_under_the_same_key_is_refused(concurrent):
    acme, _ = _tenants()
    key = _key()

    a, b = await asyncio.gather(
        handlers.handle_enrich(
            acme, _single_variation("enrich_request_same_key_variant_a.json", key)
        ),
        handlers.handle_enrich(
            acme, _single_variation("enrich_request_same_key_variant_b.json", key)
        ),
    )

    states = sorted([a["state"], b["state"]])
    assert states == ["completed", "failed"], f"both requests answered as bound: {states}"
    refused = a if a["state"] == "failed" else b
    assert (refused["failure_reason"] or "").startswith("idempotency_key_conflict")
