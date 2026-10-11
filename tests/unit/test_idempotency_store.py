"""EIE-FIX-001 — IdempotencyStore and the orchestrator's replay contract.

A deterministic in-memory Redis stands in for the server here, so this suite
runs on every runner. The same contract is driven end to end against a real
Redis, through the SDK handlers and the HTTP route, in
tests/integration/test_redis_idempotency_tenant_isolation.py.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.engines.enrichment_orchestrator import enrich_batch, enrich_entity
from app.models.schemas import EnrichRequest
from app.services.idempotency import (
    KEY_PREFIX,
    IdempotencyConflictError,
    IdempotencyStore,
    api_key_scope,
    request_fingerprint,
    tenant_scope,
)
from app.services.perplexity_client import SonarResponse

pytestmark = [pytest.mark.unit]


class FakeRedis:
    """The subset of redis.asyncio the store uses: GET, SET EX NX, expiry."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}

    async def get(self, name: str) -> str | None:
        return self.data.get(name)

    async def set(
        self, name: str, value: str, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        if nx and name in self.data:
            return None
        self.data[name] = value
        self.ttls[name] = ex
        return True

    def expire_all(self) -> None:
        self.data.clear()

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def store(fake: FakeRedis) -> IdempotencyStore:
    real = IdempotencyStore("redis://unused:6379/0", ttl=900)
    real.client = fake  # type: ignore[assignment]
    return real


ACME = tenant_scope("acme")
GLOBEX = tenant_scope("globex")
RESPONSE = {"fields": {"polymer_type": "HDPE"}, "confidence": 0.9, "state": "completed"}


class TestStore:
    async def test_a_binding_is_invisible_to_another_scope(self, store):
        await store.set(ACME, "k", "fp", RESPONSE)
        assert await store.get(ACME, "k", "fp") == RESPONSE
        assert await store.get(GLOBEX, "k", "fp") is None

    async def test_the_same_key_in_one_scope_for_another_request_is_a_conflict(self, store):
        await store.set(ACME, "k", "fp-a", RESPONSE)
        with pytest.raises(IdempotencyConflictError):
            await store.get(ACME, "k", "fp-b")

    async def test_the_first_binding_is_never_replaced(self, store):
        await store.set(ACME, "k", "fp-a", RESPONSE)
        with pytest.raises(IdempotencyConflictError):
            await store.set(ACME, "k", "fp-b", {"fields": {"other": 1}})
        assert await store.get(ACME, "k", "fp-a") == RESPONSE

    async def test_winning_the_binding_returns_this_response(self, store):
        assert await store.set(ACME, "k", "fp", RESPONSE) == RESPONSE

    async def test_losing_to_the_same_request_returns_the_winners_response(self, store):
        """A retry in flight alongside the original: whoever completes second
        answers with what the key holds, not with its own unbound result."""
        await store.set(ACME, "k", "fp", RESPONSE)
        assert await store.set(ACME, "k", "fp", {"fields": {"late": 1}}) == RESPONSE

    async def test_every_binding_carries_the_configured_ttl(self, store, fake):
        await store.set(ACME, "k", "fp", RESPONSE)
        assert list(fake.ttls.values()) == [900]

    async def test_an_expired_binding_is_a_miss(self, store, fake):
        await store.set(ACME, "k", "fp", RESPONSE)
        fake.expire_all()
        assert await store.get(ACME, "k", "fp") is None

    async def test_neither_tenant_nor_caller_key_is_readable_in_the_redis_key(self, store, fake):
        await store.set(tenant_scope("acme-corp-secret"), "order-4711", "fp", RESPONSE)
        (redis_key,) = fake.data
        assert redis_key.startswith(KEY_PREFIX)
        assert "acme-corp-secret" not in redis_key
        assert "order-4711" not in redis_key

    async def test_a_record_in_the_pre_scope_layout_is_never_replayed(self, store, fake):
        """Old entries were the bare response under enrich:idem:{key}; a value of
        that shape found under a v2 key must not be served as anyone's result."""
        await store.set(ACME, "k", "fp", RESPONSE)
        (redis_key,) = fake.data
        fake.data[redis_key] = json.dumps(RESPONSE)
        assert await store.get(ACME, "k", "fp") is None

    async def test_an_unparseable_record_is_a_miss(self, store, fake):
        await store.set(ACME, "k", "fp", RESPONSE)
        (redis_key,) = fake.data
        fake.data[redis_key] = "not json"
        assert await store.get(ACME, "k", "fp") is None

    async def test_the_legacy_key_layout_is_not_read(self, store, fake):
        fake.data["enrich:idem:k"] = json.dumps(RESPONSE)
        assert await store.get(ACME, "k", "fp") is None


class TestScopes:
    def test_a_tenant_cannot_name_itself_into_the_http_scope(self):
        assert tenant_scope(api_key_scope("key")) != api_key_scope("key")

    def test_the_http_scope_holds_no_raw_key(self):
        assert "super-secret" not in api_key_scope("super-secret")

    def test_fingerprint_ignores_key_order(self):
        assert request_fingerprint({"a": 1, "b": {"c": 2, "d": 3}}) == request_fingerprint(
            {"b": {"d": 3, "c": 2}, "a": 1}
        )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


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
        return_value={"context_text": "", "content_hash": "kb0", "fragment_ids": [], "kb_files": []}
    )
    return resolver


def _request(**overrides: Any) -> EnrichRequest:
    base: dict[str, Any] = {
        "entity": {"Name": "Acme Recycling"},
        "object_type": "Account",
        "objective": "Determine polymer type",
        "max_variations": 1,
        "idempotency_key": "op-1",
    }
    base.update(overrides)
    return EnrichRequest.model_validate(base)


def _provider(value: str) -> AsyncMock:
    return AsyncMock(
        return_value=SonarResponse(
            data={"confidence": 0.9, "polymer_type": value},
            tokens_used=5,
            citations=[],
            model="sonar",
        )
    )


async def _enrich(request, settings, kb, store, value, **kwargs):
    provider = _provider(value)
    with patch("app.engines.enrichment_orchestrator.query_perplexity", provider):
        response = await enrich_entity(request, settings, kb, store, **kwargs)
    return response, provider


class TestOrchestrator:
    async def test_replay_is_per_scope(self, settings, kb, store):
        a, _ = await _enrich(_request(), settings, kb, store, "HDPE", idempotency_scope=ACME)
        b, provider = await _enrich(_request(), settings, kb, store, "PP", idempotency_scope=GLOBEX)
        assert a.fields == {"polymer_type": "HDPE"}
        provider.assert_awaited()
        assert b.fields == {"polymer_type": "PP"}

    async def test_an_identical_retry_replays(self, settings, kb, store):
        first, _ = await _enrich(_request(), settings, kb, store, "HDPE", idempotency_scope=ACME)
        retry, provider = await _enrich(
            _request(), settings, kb, store, "PP", idempotency_scope=ACME
        )
        provider.assert_not_awaited()
        assert retry == first

    async def test_a_changed_request_under_the_same_key_is_refused(self, settings, kb, store):
        await _enrich(_request(), settings, kb, store, "HDPE", idempotency_scope=ACME)
        changed = _request(objective="Determine contamination percentage")
        refused, provider = await _enrich(
            changed, settings, kb, store, "PP", idempotency_scope=ACME
        )
        provider.assert_not_awaited()
        assert refused.state == "failed"
        assert (refused.failure_reason or "").startswith("idempotency_key_conflict")
        assert not refused.fields

    async def test_a_failed_enrichment_is_not_bound(self, settings, kb, store, fake):
        failing = AsyncMock(side_effect=RuntimeError("provider down"))
        with patch("app.engines.enrichment_orchestrator.query_perplexity", failing):
            response = await enrich_entity(_request(), settings, kb, store, idempotency_scope=ACME)
        assert response.state == "failed"
        assert fake.data == {}

    async def test_an_unscoped_key_is_neither_replayed_nor_recorded(
        self, settings, kb, store, fake
    ):
        await store.set(ACME, "op-1", "irrelevant", RESPONSE)
        before = dict(fake.data)
        response, provider = await _enrich(_request(), settings, kb, store, "PP")
        provider.assert_awaited()
        assert response.fields == {"polymer_type": "PP"}
        assert fake.data == before

    async def test_caching_disabled_runs_normally(self, settings, kb):
        response, provider = await _enrich(
            _request(), settings, kb, None, "PP", idempotency_scope=ACME
        )
        provider.assert_awaited()
        assert response.state == "completed"

    async def test_no_key_bypasses_the_store(self, settings, kb, store, fake):
        response, provider = await _enrich(
            _request(idempotency_key=None), settings, kb, store, "PP", idempotency_scope=ACME
        )
        provider.assert_awaited()
        assert response.state == "completed"
        assert fake.data == {}

    async def test_a_lost_race_to_the_same_request_answers_with_the_binding(
        self, settings, kb, store
    ):
        """The duplicate completed first while this call was enriching."""
        winner = {"fields": {"polymer_type": "WINNER"}, "confidence": 0.9, "state": "completed"}
        fingerprint = request_fingerprint(
            _request().model_dump(mode="json", exclude={"idempotency_key"})
        )

        async def concurrent_duplicate_completes(**_: Any) -> SonarResponse:
            await store.set(ACME, "op-1", fingerprint, winner)
            return SonarResponse(
                data={"confidence": 0.9, "polymer_type": "LOSER"},
                tokens_used=5,
                citations=[],
                model="sonar",
            )

        with patch(
            "app.engines.enrichment_orchestrator.query_perplexity", concurrent_duplicate_completes
        ):
            response = await enrich_entity(_request(), settings, kb, store, idempotency_scope=ACME)
        assert response.fields == {"polymer_type": "WINNER"}

    async def test_a_lost_race_to_a_different_request_is_a_conflict(self, settings, kb, store):
        async def other_request_completes(**_: Any) -> SonarResponse:
            await store.set(ACME, "op-1", "another-request", RESPONSE)
            return SonarResponse(
                data={"confidence": 0.9, "polymer_type": "LOSER"},
                tokens_used=5,
                citations=[],
                model="sonar",
            )

        with patch("app.engines.enrichment_orchestrator.query_perplexity", other_request_completes):
            response = await enrich_entity(_request(), settings, kb, store, idempotency_scope=ACME)
        assert response.state == "failed"
        assert (response.failure_reason or "").startswith("idempotency_key_conflict")
        assert not response.fields

    async def test_batch_carries_the_scope_to_every_entity(self, settings, kb, store):
        await _enrich(_request(), settings, kb, store, "HDPE", idempotency_scope=ACME)
        provider = _provider("PP")
        with patch("app.engines.enrichment_orchestrator.query_perplexity", provider):
            (result,) = await enrich_batch(
                [_request()], settings, kb, store, idempotency_scope=GLOBEX
            )
        provider.assert_awaited()
        assert result.fields == {"polymer_type": "PP"}
