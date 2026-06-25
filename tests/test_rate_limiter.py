"""
Regression test — RateLimitMiddleware must return a real 429 to the client.

Raising HTTPException inside a Starlette BaseHTTPMiddleware.dispatch does not
pass through FastAPI's exception handlers; it propagates to ServerErrorMiddleware
and surfaces as a 500. The middleware must return a JSONResponse instead so the
client receives the intended 429 Too Many Requests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.rate_limiter import RateLimitMiddleware


@pytest.fixture(scope="module", autouse=True)
def _env() -> Iterator[None]:
    """Scope test env vars to this module instead of mutating os.environ globally.

    A module-level ``os.environ.update`` leaks into every other test in the
    session and causes order-dependent flakiness. ``mock.patch.dict`` restores
    the original environment on teardown.
    """
    with mock.patch.dict(
        os.environ,
        {
            "PERPLEXITY_API_KEY": "test-key",
            "API_SECRET_KEY": "test-secret-key-32-chars-long!!",
            "API_KEY_HASH": "d74ff0ee8da3b9806b18c877dbf29bbde50b5bd8e4dad7a3a725000feb82e8f1",
            "KB_DIR": "./kb",
            "REDIS_URL": "redis://localhost:6379/0",
        },
    ):
        yield


def _build_client(rpm: int) -> TestClient:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_minute=rpm)

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"status": "ok"}

    return TestClient(app)


class TestRateLimitMiddleware:
    def test_returns_429_when_limit_exceeded(self):
        client = _build_client(rpm=2)
        headers = {"X-API-Key": "client-a"}

        assert client.get("/ping", headers=headers).status_code == 200
        assert client.get("/ping", headers=headers).status_code == 200

        rejected = client.get("/ping", headers=headers)
        # Must be a real 429 — never a 500 from a propagated HTTPException.
        assert rejected.status_code == 429
        assert rejected.json()["detail"] == "Rate limit: 2 requests/minute"

    def test_429_advertises_retry_after(self):
        client = _build_client(rpm=1)
        headers = {"X-API-Key": "client-b"}

        assert client.get("/ping", headers=headers).status_code == 200

        rejected = client.get("/ping", headers=headers)
        assert rejected.status_code == 429
        # RFC 6585/9110: clients must be told when to retry.
        retry_after = rejected.headers["Retry-After"]
        assert 1 <= int(retry_after) <= 60

    def test_separate_keys_have_independent_windows(self):
        client = _build_client(rpm=1)

        assert client.get("/ping", headers={"X-API-Key": "key-1"}).status_code == 200
        # Different key — fresh window, not rejected.
        assert client.get("/ping", headers={"X-API-Key": "key-2"}).status_code == 200
        # Same key again — over the limit.
        assert client.get("/ping", headers={"X-API-Key": "key-1"}).status_code == 429
