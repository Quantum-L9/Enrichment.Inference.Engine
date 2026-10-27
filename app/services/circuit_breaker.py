"""
Three-state circuit breaker: closed → open → half_open → closed.

Audit fix C2: Module-level singleton shared across all requests
within a worker process. Not per-request instantiation.

Audit fix M4: Logs state transitions.

F-023: half_open admits exactly one probe. The probe's outcome decides the
next state: success closes the breaker, failure reopens it for a fresh
cooldown. Thresholds come from ``CB_FAILURE_THRESHOLD`` /
``CB_COOLDOWN_SECONDS`` via :meth:`CircuitBreaker.from_settings`.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

logger = structlog.get_logger("circuit_breaker")


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, cooldown: int = 60):
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self.failures = 0
        self.last_failure: float = 0.0
        self.state = "closed"
        self.probe_started: float = 0.0

    @classmethod
    def from_settings(cls, settings: Any) -> CircuitBreaker:
        return cls(
            failure_threshold=int(settings.cb_failure_threshold),
            cooldown=int(settings.cb_cooldown_seconds),
        )

    def record_failure(self) -> None:
        self.failures += 1
        self.last_failure = time.monotonic()
        if self.state == "half_open":
            # The single probe failed: reopen for a fresh cooldown.
            self.state = "open"
            logger.warning("circuit_breaker_reopened", failures=self.failures)
            return
        if self.failures >= self.failure_threshold and self.state != "open":
            self.state = "open"
            logger.warning(
                "circuit_breaker_opened",
                failures=self.failures,
                cooldown=self.cooldown,
            )

    def record_success(self) -> None:
        if self.state != "closed":
            logger.info("circuit_breaker_closed", previous_state=self.state)
        self.failures = 0
        self.state = "closed"

    def allow(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.monotonic() - self.last_failure > self.cooldown:
                self.state = "half_open"
                self.probe_started = time.monotonic()
                logger.info("circuit_breaker_half_open")
                return True
            return False
        # half_open: the one probe is already out; everyone else waits for it.
        # A probe that never reported back (e.g. its caller was cancelled) must
        # not wedge the breaker, so after one cooldown a fresh probe is admitted.
        if time.monotonic() - self.probe_started > self.cooldown:
            self.probe_started = time.monotonic()
            return True
        return False

    @property
    def is_open(self) -> bool:
        return self.state == "open"
