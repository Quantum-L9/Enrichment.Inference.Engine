"""EIE-003 — process-local state and multi-process serving cannot both be true.

``GraphReturnChannel`` keeps queued enrichment targets in a class-level
singleton holding ``asyncio.Queue`` objects. Each uvicorn ``--workers`` entry is
a separate OS process with its own Python memory, so a second worker gets its
own empty channel: a ``graph-inference-result`` packet delivered to worker A is
invisible to a convergence loop draining the channel in worker B, and the result
is dropped as a function of which worker the load balancer picked. Registration
is likewise repeated once per worker.

The Docker images shipped ``--workers 4`` (dev) and ``--workers 2`` (prod). The
E2E rail did not fail on it only because its harness overrode the count to 1.

Either side may change — move the channel to shared state (Redis is already a
dependency) and more workers become correct. Until that happens these two facts
must stay consistent with each other, which is what this module asserts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = ("Dockerfile", "Dockerfile.prod")
WORKERS_RE = re.compile(r'"--workers",\s*"(?P<count>\d+)"')


@pytest.mark.parametrize("name", DOCKERFILES)
def test_image_serves_with_a_single_worker(name: str) -> None:
    text = (ROOT / name).read_text(encoding="utf-8")
    counts = [int(m.group("count")) for m in WORKERS_RE.finditer(text)]
    assert counts, f"{name} declares no --workers count"
    assert all(count == 1 for count in counts), (
        f"{name} serves with {counts} workers while GraphReturnChannel is a "
        "per-process singleton; move the channel to shared state before raising this"
    )


def test_the_return_channel_is_still_process_local() -> None:
    """If this ever fails, the invariant above can be revisited — not before."""
    from app.services.graph_return_channel import GraphReturnChannel

    channel = GraphReturnChannel.get_instance()
    try:
        assert GraphReturnChannel.get_instance() is channel, "singleton per process"
        assert hasattr(channel, "_queues"), "queues are held in process memory"
    finally:
        GraphReturnChannel.reset_instance()
