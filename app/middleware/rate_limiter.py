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
from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window per-API-key rate limiter.

    Chassis responsibility: enforces request-rate governance before the
    request reaches any engine handler.
    """

    def __init__(self, app, requests_per_minute: int = 120) -> None:
        """Bind the middleware to ``app`` with a per-key request ceiling."""
        super().__init__(app)
        self.rpm = requests_per_minute
        self.windows: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        """Enforce the per-key sliding window, returning a 429 when exceeded."""
        key = request.headers.get(
            "X-API-Key",
            request.client.host if request.client else "unknown",
        )

        now = time.time()
        cutoff = now - 60
        pruned = [t for t in self.windows[key] if t > cutoff]
        if pruned:
            self.windows[key] = pruned
        else:
            # Evict keys whose window emptied out. Without this, self.windows
            # accumulates one entry per distinct X-API-Key / client IP for the
            # life of the process — an unbounded-growth / memory-leak vector
            # under high key cardinality or rotating/spoofed keys. defaultdict
            # re-creates the entry below if a new request arrives.
            self.windows.pop(key, None)

        if len(self.windows[key]) >= self.rpm:
            # Return the response directly. Raising HTTPException inside a
            # BaseHTTPMiddleware.dispatch does NOT pass through FastAPI's
            # exception handlers — it propagates to ServerErrorMiddleware and
            # surfaces as a 500. JSONResponse gives the client the real 429.
            #
            # Per RFC 6585/9110, advertise Retry-After so clients know when the
            # sliding window will admit them again: the oldest in-window
            # timestamp + 60s, floored at 1s.
            window = self.windows[key]
            retry_after = max(1, int(window[0] + 60 - now)) if window else 60
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": f"Rate limit: {self.rpm} requests/minute"},
                headers={"Retry-After": str(retry_after)},
            )

        self.windows[key].append(now)
        return await call_next(request)
