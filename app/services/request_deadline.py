"""Monotonic end-to-end request deadline for the canonical Odoo converge path.

Odoo calls Gate with a 30 s budget. EIE must finish the *complete* convergence
request — preprocessing, provider work, retries, convergence logic, persistence
and response assembly — inside a smaller budget, leaving enough time to return
cleanly to Gate/Odoo.

One deadline is shared by the whole request. Every provider attempt derives its
transport timeout from what is left of that single deadline, so a retry loop can
never multiply a fixed per-attempt timeout past the caller's budget.

The deadline lives in a ContextVar rather than in every function signature:
`asyncio.gather` and `asyncio.to_thread` both copy the current context, so a
deadline installed by the canonical handler is visible to the variation tasks
and to the blocking SDK worker thread without threading a parameter through the
convergence controller's public API.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

# Odoo caller budget is 30 s. EIE keeps 25 s so Gate hop + error propagation fit.
CANONICAL_CONVERGE_BUDGET_SECONDS = 25.0

# Held back from provider work so a timing-out request can still assemble and
# return a structured response/error to Gate instead of being killed mid-write.
RESPONSE_RESERVE_SECONDS = 2.0

# Hard ceiling on a single provider attempt, independent of remaining budget.
PROVIDER_ATTEMPT_CAP_SECONDS = 20.0

# An attempt shorter than this cannot plausibly complete; do not start one.
MIN_PROVIDER_ATTEMPT_SECONDS = 1.0


@dataclass(frozen=True)
class Deadline:
    """A monotonic end-to-end deadline with a reserved tail for the response."""

    expires_at: float
    reserve: float

    @classmethod
    def start(
        cls,
        budget_seconds: float = CANONICAL_CONVERGE_BUDGET_SECONDS,
        reserve_seconds: float = RESPONSE_RESERVE_SECONDS,
    ) -> Deadline:
        """Open a deadline `budget_seconds` from now, measured monotonically."""
        return cls(expires_at=time.monotonic() + budget_seconds, reserve=reserve_seconds)

    def remaining(self) -> float:
        """Seconds until the deadline itself. May be negative."""
        return self.expires_at - time.monotonic()

    def remaining_for_work(self) -> float:
        """Seconds usable for work, excluding the response/error reserve."""
        return self.remaining() - self.reserve

    def expired(self) -> bool:
        """True once no usable work time is left."""
        return self.remaining_for_work() <= 0.0

    def attempt_timeout(
        self,
        cap_seconds: float = PROVIDER_ATTEMPT_CAP_SECONDS,
        min_seconds: float = MIN_PROVIDER_ATTEMPT_SECONDS,
    ) -> float | None:
        """Transport timeout for the next attempt, or None when there is no room.

        Returning None is the E5 guard: a new provider attempt must not start if
        the remaining budget cannot accommodate it plus the response reserve.
        """
        budget = min(cap_seconds, self.remaining_for_work())
        if budget < min_seconds:
            return None
        return budget


_deadline: ContextVar[Deadline | None] = ContextVar("eie_request_deadline", default=None)


def current_deadline() -> Deadline | None:
    """The deadline governing the in-flight request, if one was installed."""
    return _deadline.get()


@contextmanager
def deadline_scope(deadline: Deadline) -> Iterator[Deadline]:
    """Install `deadline` for the duration of the block.

    Tasks created inside the block inherit it, because asyncio copies the
    current context when it creates a task.
    """
    token = _deadline.set(deadline)
    try:
        yield deadline
    finally:
        _deadline.reset(token)


def provider_attempt_timeout(fallback_seconds: float) -> float | None:
    """Transport timeout for the next provider attempt.

    With a deadline installed, the value is derived from what is left of it.
    Without one (non-canonical callers), fall back to the configured ceiling —
    still a real bound, where the SDK default would otherwise be 900 s.
    """
    deadline = current_deadline()
    if deadline is None:
        return max(fallback_seconds, MIN_PROVIDER_ATTEMPT_SECONDS)
    return deadline.attempt_timeout()
