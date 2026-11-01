"""WU-20 / F-023: the provider circuit breaker is fed, configurable, and probes once."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import circuit_breaker as cb_module
from app.services import perplexity_client
from app.services.circuit_breaker import CircuitBreaker


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(cb_module.time, "monotonic", c)
    return c


def _trip(breaker: CircuitBreaker) -> None:
    for _ in range(breaker.failure_threshold):
        breaker.record_failure()


def test_opens_after_threshold_consecutive_failures(clock: _Clock) -> None:
    breaker = CircuitBreaker(failure_threshold=3, cooldown=10)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.allow() is False


def test_success_resets_the_consecutive_count(clock: _Clock) -> None:
    breaker = CircuitBreaker(failure_threshold=3, cooldown=10)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed"


def test_stays_closed_to_traffic_until_cooldown(clock: _Clock) -> None:
    breaker = CircuitBreaker(failure_threshold=2, cooldown=10)
    _trip(breaker)
    clock.now += 9
    assert breaker.allow() is False
    clock.now += 2
    assert breaker.allow() is True
    assert breaker.state == "half_open"


def test_half_open_admits_exactly_one_probe(clock: _Clock) -> None:
    breaker = CircuitBreaker(failure_threshold=2, cooldown=10)
    _trip(breaker)
    clock.now += 11
    admitted = [breaker.allow() for _ in range(5)]
    assert admitted == [True, False, False, False, False]


def test_probe_success_closes(clock: _Clock) -> None:
    breaker = CircuitBreaker(failure_threshold=2, cooldown=10)
    _trip(breaker)
    clock.now += 11
    assert breaker.allow() is True
    breaker.record_success()
    assert breaker.state == "closed"
    assert all(breaker.allow() for _ in range(3))


def test_probe_failure_reopens_for_a_fresh_cooldown(clock: _Clock) -> None:
    breaker = CircuitBreaker(failure_threshold=2, cooldown=10)
    _trip(breaker)
    clock.now += 11
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state == "open"
    clock.now += 5
    assert breaker.allow() is False


def test_unreported_probe_does_not_wedge_half_open(clock: _Clock) -> None:
    breaker = CircuitBreaker(failure_threshold=2, cooldown=10)
    _trip(breaker)
    clock.now += 11
    assert breaker.allow() is True
    clock.now += 5
    assert breaker.allow() is False
    clock.now += 6
    assert breaker.allow() is True
    assert breaker.allow() is False


def test_from_settings_reads_cb_configuration() -> None:
    breaker = CircuitBreaker.from_settings(
        SimpleNamespace(cb_failure_threshold=7, cb_cooldown_seconds=42)
    )
    assert breaker.failure_threshold == 7
    assert breaker.cooldown == 42


def test_orchestrator_breaker_uses_cb_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import Settings

    monkeypatch.setenv("CB_FAILURE_THRESHOLD", "3")
    monkeypatch.setenv("CB_COOLDOWN_SECONDS", "9")
    breaker = CircuitBreaker.from_settings(Settings())
    assert (breaker.failure_threshold, breaker.cooldown) == (3, 9)


def _sonar() -> perplexity_client.SonarResponse:
    return perplexity_client.SonarResponse(data={}, tokens_used=1, citations=[], model="sonar")


def test_provider_failures_feed_the_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: Any) -> perplexity_client.SonarResponse:
        raise ConnectionError("provider down")

    monkeypatch.setattr(perplexity_client, "_sync_call", boom)
    breaker = CircuitBreaker(failure_threshold=2, cooldown=60)

    for _ in range(2):
        with pytest.raises(ConnectionError):
            asyncio.run(perplexity_client.query_perplexity({}, "k", breaker=breaker))

    assert breaker.state == "open"
    with pytest.raises(RuntimeError, match="circuit_open"):
        asyncio.run(perplexity_client.query_perplexity({}, "k", breaker=breaker))


def test_provider_success_feeds_the_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(perplexity_client, "_sync_call", lambda *_: _sonar())
    breaker = CircuitBreaker(failure_threshold=2, cooldown=60)
    breaker.record_failure()

    asyncio.run(perplexity_client.query_perplexity({}, "k", breaker=breaker))

    assert breaker.failures == 0
    assert breaker.state == "closed"


def test_cancellation_is_not_a_provider_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request deadline cancelling sibling variations says nothing about the provider."""

    def slow(*_: Any) -> perplexity_client.SonarResponse:
        import time

        time.sleep(0.2)
        return _sonar()

    monkeypatch.setattr(perplexity_client, "_sync_call", slow)
    breaker = CircuitBreaker(failure_threshold=1, cooldown=60)

    async def cancel_it() -> None:
        task = asyncio.ensure_future(perplexity_client.query_perplexity({}, "k", breaker=breaker))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_it())
    assert breaker.failures == 0
    assert breaker.state == "closed"
