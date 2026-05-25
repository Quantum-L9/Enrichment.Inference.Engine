"""
Sliding-window rate limiter — chassis layer only.

In-memory for single worker; swap to Redis ZRANGEBYSCORE for multi-worker.

L9 Architecture Note:
This module is CHASSIS. It is the only correct location for FastAPI
middleware imports (INV-ARCH-03). Do not replicate this pattern in
engine modules.

# L9-fix: ARCH-001
# L9-file: app/middleware/rate_limiter.py
# L9-violation: Missing chassis-layer metadata tag — ARCH-001 scanner fired on FastAPI imports
# L9-fix-summary: Added # L9-layer: chassis header and architecture note per INV-ARCH-03
# L9-layer: chassis
# L9-node: enrichment-inference-engine
# L9-contract-version: 1.0.0
"""

from __future__ import annotations

import time
from collections import defaultdict

# FastAPI/Starlette imports are PERMITTED here — this module lives in the
# chassis layer, which owns HTTP middleware (§2.1, INV-ARCH-03).
from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window per-API-key rate limiter.

    Chassis responsibility: enforces request-rate governance before the
    request reaches any engine handler.
    """

    def __init__(self, app, requests_per_minute: int = 120) -> None:
        """
        Initialize the RateLimitMiddleware and configure its in-memory sliding-window state.
        
        Parameters:
            requests_per_minute (int): Maximum allowed requests per 60-second rolling window for each key.
        
        The middleware stores the configured limit on self.rpm and initializes self.windows
        as a mapping from key (API key or client identifier) to a list of request timestamps.
        """
        super().__init__(app)
        self.rpm = requests_per_minute
        self.windows: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        """
        Enforce a per-key sliding-window rate limit (60 seconds) and either forward the request or raise HTTP 429.
        
        Derives a key from the X-API-Key header, falling back to the client IP or "unknown", retains timestamps within the last 60 seconds, and rejects the request if the count is greater than or equal to the configured requests-per-minute; otherwise records the current timestamp and forwards the request.
        
        Parameters:
            request (Request): Incoming Starlette/FastAPI request.
            call_next: Callable that receives the request and returns the downstream response.
        
        Returns:
            The response returned by the next application in the ASGI/FastAPI stack.
        
        Raises:
            HTTPException: Raised with status 429 (Too Many Requests) when the key has reached the allowed rate.
        """
        key = request.headers.get(
            "X-API-Key",
            request.client.host if request.client else "unknown",
        )

        now = time.time()
        cutoff = now - 60
        self.windows[key] = [t for t in self.windows[key] if t > cutoff]

        if len(self.windows[key]) >= self.rpm:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit: {self.rpm} requests/minute",
            )

        self.windows[key].append(now)
        return await call_next(request)
