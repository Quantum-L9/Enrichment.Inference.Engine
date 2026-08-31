"""Launch contract for the canonical Odoo -> Gate -> EIE converge request.

Odoo waits 30 s. These tests pin the three properties that budget depends on:

1. the complete EIE request terminates inside its own 25 s budget;
2. EIE is the only retry owner — the Perplexity SDK adds none;
3. a successful canonical convergence makes zero Graph calls.

The transport test deliberately does not assert against a mock that raises
`APITimeoutError` on demand. That proves only that the code can catch an
exception it was handed. The defect being regressed here was that the timeout
never reached the network at all, so the proof has to be a real socket that
accepts the request and then stalls.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from perplexity import APITimeoutError, Perplexity

from app.engines.handlers import _handle_odoo_converge
from app.models.schemas import EnrichResponse
from app.services import perplexity_client
from app.services.odoo_gate_converge import DEFAULT_CONVERGE_TIMEOUT_SECONDS
from app.services.request_deadline import (
    CANONICAL_CONVERGE_BUDGET_SECONDS,
    RESPONSE_RESERVE_SECONDS,
    Deadline,
    deadline_scope,
    provider_attempt_timeout,
)

pytestmark = pytest.mark.unit

STALL_SECONDS = 4.0
PROVIDER_TIMEOUT = 1.0


# ── A real transport that accepts, then stalls ─────────────


class _StallingHandler(BaseHTTPRequestHandler):
    """Accepts the POST, reads the body, then never answers in time."""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)
        self.server.requests_seen.append(self.path)  # type: ignore[attr-defined]
        time.sleep(STALL_SECONDS)
        with contextlib.suppress(OSError):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    def log_message(self, *args: Any) -> None:
        """Silence the default stderr access log."""
        return


@pytest.fixture
def stalling_provider():
    """A local HTTP server that accepts a chat completion and stalls on it."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StallingHandler)
    # Do not join stalled handler threads at teardown; the stall is the point.
    server.daemon_threads = True
    server.requests_seen = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]

    api_key = "stall-test-key"
    perplexity_client._clients[api_key] = Perplexity(
        api_key=api_key,
        base_url=f"http://{host}:{port}",
        max_retries=0,
    )
    try:
        yield api_key, server
    finally:
        perplexity_client._clients.pop(api_key, None)
        server.shutdown()
        server.server_close()


def _payload() -> dict[str, Any]:
    return {
        "model": "sonar",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 16,
    }


def _odoo_request() -> dict[str, Any]:
    return {
        "entity_id": "res.partner:55",
        "domain": "plasticos",
        "entity_snapshot": {"name": "Acme Recycling", "city": "Charlotte"},
        "odoo": {
            "model": "plasticos.enrichment.run",
            "record_id": 7,
            "correlation_id": "plasticos.enrichment.run:7",
        },
    }


def _completed_response(**overrides: Any) -> EnrichResponse:
    base: dict[str, Any] = {
        "fields": {"website": "https://acme.example", "zip": "28202"},
        "confidence": 0.88,
        "pass_count": 2,
        "tokens_used": 1200,
        "state": "completed",
    }
    base.update(overrides)
    return EnrichResponse(**base)


# ── 4. Real provider transport timeout ─────────────────────


def test_provider_transport_timeout_is_applied_to_the_real_request(stalling_provider):
    """The configured timeout reaches the socket, not just a function parameter."""
    api_key, server = stalling_provider

    started = time.monotonic()
    with pytest.raises(APITimeoutError):
        perplexity_client._sync_call(_payload(), api_key, PROVIDER_TIMEOUT)
    elapsed = time.monotonic() - started

    assert server.requests_seen, "the server never received the request — nothing was proven"
    # Bounded by the timeout, not by the server's stall.
    assert elapsed < STALL_SECONDS / 2, f"transport ran {elapsed:.2f}s; timeout was not applied"
    assert elapsed >= PROVIDER_TIMEOUT * 0.5


# ── 3. SDK retries disabled ────────────────────────────────


def test_pooled_client_is_constructed_with_sdk_retries_disabled():
    """`_get_client` must not inherit the SDK's default max_retries=2."""
    api_key = "retry-config-probe"
    perplexity_client._clients.pop(api_key, None)
    try:
        client = perplexity_client._get_client(api_key)
        assert client.max_retries == 0
    finally:
        perplexity_client._clients.pop(api_key, None)


def test_stalled_provider_issues_exactly_one_http_request(stalling_provider):
    """EIE owns retries: a transport timeout is not silently retried by the SDK."""
    api_key, server = stalling_provider

    with pytest.raises(APITimeoutError):
        perplexity_client._sync_call(_payload(), api_key, PROVIDER_TIMEOUT)

    assert len(server.requests_seen) == 1, (
        f"expected one provider request, saw {len(server.requests_seen)} — "
        "a second retry owner is active"
    )


def test_every_request_forces_max_retries_zero():
    """Even a client mutated elsewhere cannot reintroduce SDK retries."""
    api_key = "with-options-probe"
    seen: dict[str, Any] = {}

    class _Recorder:
        max_retries = 2

        def with_options(self, **kwargs: Any):
            seen.update(kwargs)
            return _Configured()

    class _Configured:
        @property
        def chat(self):
            raise RuntimeError("stop-after-configuration")

    perplexity_client._clients[api_key] = _Recorder()  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="stop-after-configuration"):
            perplexity_client._sync_call(_payload(), api_key, 5.0)
    finally:
        perplexity_client._clients.pop(api_key, None)

    assert seen["max_retries"] == 0
    assert seen["timeout"] == pytest.approx(5.0)


# ── 6/7. Retry budget bounded by one shared deadline ───────


def test_attempt_timeout_derives_from_remaining_budget():
    """A provider attempt is sized by what is left, not by a fixed constant."""
    deadline = Deadline.start(6.0)
    with deadline_scope(deadline):
        resolved = provider_attempt_timeout(120.0)
    assert resolved is not None
    assert resolved <= 6.0 - RESPONSE_RESERVE_SECONDS
    # Never the 120 s fallback: the deadline, not the setting, binds.
    assert resolved < 120.0


def test_retry_is_allowed_while_budget_remains():
    """An early retryable failure may retry when the deadline still allows it."""
    api_key = "retry-allowed-probe"
    calls: list[float] = []

    class _Flaky:
        def with_options(self, **kwargs: Any):
            calls.append(kwargs["timeout"])
            return self

        @property
        def chat(self):
            raise ConnectionResetError("transient")

    perplexity_client._clients[api_key] = _Flaky()  # type: ignore[assignment]
    try:
        with (
            deadline_scope(Deadline.start(CANONICAL_CONVERGE_BUDGET_SECONDS)),
            pytest.raises(ConnectionResetError),
        ):
            perplexity_client._sync_call(_payload(), api_key, 20.0)
    finally:
        perplexity_client._clients.pop(api_key, None)

    assert len(calls) == perplexity_client._MAX_RETRIES
    # Each attempt is sized from the shrinking shared deadline, never 3 x 20 s.
    assert calls == sorted(calls, reverse=True)
    assert all(t <= CANONICAL_CONVERGE_BUDGET_SECONDS for t in calls)


def test_no_attempt_starts_once_the_budget_is_exhausted():
    """E5: below the response reserve, no further provider request is issued."""
    api_key = "exhausted-probe"
    calls: list[float] = []

    class _NeverReached:
        def with_options(self, **kwargs: Any):
            calls.append(kwargs["timeout"])
            return self

        @property
        def chat(self):
            raise AssertionError("provider was called with no budget left")

    perplexity_client._clients[api_key] = _NeverReached()  # type: ignore[assignment]
    try:
        # Budget already below the reserve.
        with (
            deadline_scope(Deadline.start(RESPONSE_RESERVE_SECONDS / 2)),
            pytest.raises(TimeoutError),
        ):
            perplexity_client._sync_call(_payload(), api_key, 20.0)
    finally:
        perplexity_client._clients.pop(api_key, None)

    assert calls == []


# ── 8. Event-loop safety ───────────────────────────────────


async def test_blocking_provider_cannot_defeat_the_outer_deadline():
    """A stalled synchronous provider call must not block the event loop."""
    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    def _blocking(*_args: Any, **_kwargs: Any):
        time.sleep(STALL_SECONDS)
        raise AssertionError("unreachable")

    ticker = asyncio.create_task(_ticker())
    started = time.monotonic()
    try:
        with (
            patch.object(perplexity_client, "_sync_call", _blocking),
            pytest.raises(TimeoutError),
        ):
            await asyncio.wait_for(
                perplexity_client.query_perplexity(payload=_payload(), api_key="k", timeout=1.0),
                timeout=0.5,
            )
    finally:
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker

    elapsed = time.monotonic() - started
    assert elapsed < STALL_SECONDS / 2, "outer deadline did not fire while the thread stalled"
    assert ticks > 3, "the event loop was blocked by the synchronous provider call"


# ── 1/2/5/9/11/12. Canonical handler contract ──────────────


@pytest.fixture
def canonical_converge_probes():
    """Patch the canonical path's collaborators and expose the call records."""
    with (
        patch("app.engines.handlers.run_convergence_loop", new_callable=AsyncMock) as loop,
        patch("app.services.result_store.ResultStore.persist_enrich_response") as persist,
        patch("app.engines.packet_router.get_router") as router_factory,
        patch("app.services.event_emitter.get_emitter") as emitter_factory,
    ):
        loop.return_value = _completed_response()
        persist.return_value = None
        router = router_factory.return_value
        router.notify_graph_sync = AsyncMock(return_value=None)
        router.notify_score_invalidate = AsyncMock(return_value=None)
        emitter_factory.return_value.emit_enrichment_completed = AsyncMock(return_value=None)

        from app.services.side_effect_coordinator import get_side_effect_coordinator

        get_side_effect_coordinator().reset_for_tests()
        yield {"loop": loop, "persist": persist, "router": router}
        get_side_effect_coordinator().reset_for_tests()


async def test_canonical_payload_reaches_the_active_runtime_path(canonical_converge_probes):
    """The Odoo wire format routes to the Odoo converge branch, not the legacy one."""
    result = await _handle_odoo_converge("acme", _odoo_request())

    assert canonical_converge_probes["loop"].await_count == 1
    assert result["status"] == "ok"


async def test_canonical_response_shape_is_unchanged(canonical_converge_probes):
    """The Odoo-facing contract keeps state (status) and fields (final_fields)."""
    result = await _handle_odoo_converge("acme", _odoo_request())

    assert result["status"] == "ok"
    assert result["final_fields"] == {"website": "https://acme.example", "zip": "28202"}
    assert result["writeback"] == {"partner_fields": result["final_fields"]}
    for key in ("run_id", "pass_count", "total_tokens", "total_cost_usd"):
        assert key in result


async def test_successful_canonical_convergence_makes_zero_graph_calls(
    canonical_converge_probes,
):
    """E11 — the launch invariant."""
    await _handle_odoo_converge("acme", _odoo_request())

    assert canonical_converge_probes["router"].notify_graph_sync.await_count == 0


async def test_canonical_convergence_still_persists_before_responding(
    canonical_converge_probes,
):
    """E13 — Graph is excluded; required EIE persistence is not."""
    await _handle_odoo_converge("acme", _odoo_request())

    assert canonical_converge_probes["persist"].call_count == 1


async def test_canonical_request_exits_within_the_complete_budget():
    """E1/E10 — the outer deadline covers the whole operation, not one leg."""
    assert DEFAULT_CONVERGE_TIMEOUT_SECONDS <= 25.0

    async def _never_finishes(*_args: Any, **_kwargs: Any):
        await asyncio.sleep(3600)

    budget = 1.0
    with (
        patch("app.engines.handlers.run_convergence_loop", _never_finishes),
        patch("app.engines.handlers.DEFAULT_CONVERGE_TIMEOUT_SECONDS", budget),
        # The guard below is the test's own ceiling. A handler that respects its
        # deadline raises its own "exceeded ... budget" TimeoutError first; one
        # that does not trips the guard, whose TimeoutError carries no message —
        # so the match fails rather than the suite hanging.
        pytest.raises(TimeoutError, match="budget"),
    ):
        await asyncio.wait_for(
            _handle_odoo_converge("acme", _odoo_request()),
            timeout=budget + 0.5,
        )


async def test_persistence_is_inside_the_outer_deadline():
    """A stalled side-effect step cannot extend the request past its budget."""

    async def _slow_persist(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(3600)

    budget = 1.0
    with (
        patch("app.engines.handlers.run_convergence_loop", new_callable=AsyncMock) as loop,
        patch("app.engines.handlers._persist_and_sync", _slow_persist),
        patch("app.engines.handlers.DEFAULT_CONVERGE_TIMEOUT_SECONDS", budget),
    ):
        loop.return_value = _completed_response()
        # Same guard: persistence outside the deadline would hang here, so the
        # guard's unmatched TimeoutError is what marks the regression.
        with pytest.raises(TimeoutError, match="budget"):
            await asyncio.wait_for(
                _handle_odoo_converge("acme", _odoo_request()),
                timeout=budget + 0.5,
            )


# ── 10. Graph-dependent workflows still reach Graph ────────


async def test_non_canonical_enrich_still_syncs_to_graph():
    """The Graph exclusion is scoped to the canonical Odoo path only."""
    from app.services.side_effect_coordinator import get_side_effect_coordinator

    coordinator = get_side_effect_coordinator()
    coordinator.reset_for_tests()

    with (
        patch("app.services.result_store.ResultStore.persist_enrich_response"),
        patch("app.engines.packet_router.get_router") as router_factory,
        patch("app.services.event_emitter.get_emitter") as emitter_factory,
    ):
        router = router_factory.return_value
        router.notify_graph_sync = AsyncMock(return_value=None)
        router.notify_score_invalidate = AsyncMock(return_value=None)
        emitter_factory.return_value.emit_enrichment_completed = AsyncMock(return_value=None)

        report = await coordinator.commit_after_enrich(
            tenant="acme",
            entity_id="res.partner:55",
            object_type="res.partner",
            domain="plasticos",
            response_dict=_completed_response().model_dump(),
            settings=object(),
        )

    assert router.notify_graph_sync.await_count == 1
    assert report.graph_synced is True
    assert report.graph_skipped is False
    coordinator.reset_for_tests()
