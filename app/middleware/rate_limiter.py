"""
Sliding-window rate limiter.
In-memory for single worker; swap to Redis ZRANGEBYSCORE for multi-worker.

L9 Architecture Note:
    ``app/middleware/rate_limiter.py`` is a CHASSIS middleware surface.
    FastAPI/Starlette imports are permitted here because this module owns
    ingress traffic governance before execution enters engine logic.

# L9-node: enrichment-inference-engine
# L9-layer: chassis
# L9-contract-version: 1.0.0
"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, requests_per_minute: int = 120):
        super().__init__(app)
        self.rpm = requests_per_minute
        self.windows: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next):
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
