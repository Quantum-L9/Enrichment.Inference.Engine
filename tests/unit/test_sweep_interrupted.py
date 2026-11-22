"""Q9: an interrupted sweep leaves `running` deterministically, in a state the contract allows.

docs/contracts/enrichment-sweep.md: a sweep is `running` or `completed`, and an
unexpected runner error skips every candidate not yet handled `sweep_failed`
(section 5). A worker that died (restart, crash) is that error. This module
pins, without a database, the closure rule (`interrupted_progress`), that the
writer action and an in-process poll close stale sweeps without any request
or operator, that a sweep this process runs is never closed, and that a closed
sweep's late worker stops before reserving. PostgreSQL proves the staleness
query and the ledger in tests/integration/test_sweep_interrupted_pg.py.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest

from app.core.config import get_settings
from app.engines import sweep_handlers
from app.services.sweep_store import (
    COMPLETED,
    RUNNING,
    SWEEP_FAILED,
    SweepRecord,
    interrupted_progress,
)
from tests.unit.test_enrichment_sweep import (
    _ENV,
    NOW,
    READER,
    TENANT,
    Converge,
    Graph,
    MemoryStore,
    candidates,
    fetch_all,
)


@pytest.fixture(autouse=True)
def _env() -> Iterator[None]:
    with patch.dict(os.environ, _ENV):
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


def _wire(store: MemoryStore, converge: Any = None) -> None:
    sweep_handlers.init_sweep(
        reader=READER,
        store=store,
        assess=Graph(),
        converge=converge or Converge(),
        clock=lambda: NOW,
    )


async def _start(n: int, key: str) -> dict[str, Any]:
    return await sweep_handlers.handle_enrichment_sweep(
        TENANT, {"candidates": candidates(n), "daily_budget_usd": 10.0, "idempotency_key": key}
    )


def test_interrupted_progress_keeps_outcomes_and_fails_the_rest() -> None:
    record = SweepRecord(
        sweep_id="sw",
        tenant_id=TENANT,
        idempotency_key="k",
        day=NOW.date(),
        effective_budget_usd=sweep_handlers.money(1),
        candidates=tuple({"entity_id": f"rec-{i}"} for i in range(4)),
        skipped=({"entity_id": "rec-1", "reason": "readiness_absent"},),
        stats={"enriched": 1, "readiness_absent": 1},
    )
    skipped, stats = interrupted_progress(record, handled={"rec-0"})
    assert skipped == [
        {"entity_id": "rec-1", "reason": "readiness_absent"},
        {"entity_id": "rec-2", "reason": SWEEP_FAILED},
        {"entity_id": "rec-3", "reason": SWEEP_FAILED},
    ]
    assert stats == {"enriched": 1, "readiness_absent": 1, SWEEP_FAILED: 2}
    # Nothing left to fail: closing again changes nothing.
    again = replace(record, skipped=tuple(skipped), stats=stats)
    assert interrupted_progress(again, handled={"rec-0"}) == (skipped, stats)


@pytest.mark.asyncio
async def test_the_sweep_action_closes_a_stale_sweep_before_its_own_work() -> None:
    store = MemoryStore()
    # A sweep left running by a worker that is gone (it never runs here).
    first = await _start_without_running(store, "k-dead")
    store.stale.add(first)
    _wire(store)
    second = await _start(2, "k-new")
    await sweep_handlers.wait_for_background_sweeps()
    dead = store.sweeps[first]
    assert dead.state == COMPLETED
    assert {s["reason"] for s in dead.skipped} == {SWEEP_FAILED}
    page = await fetch_all(first)
    assert page["state"] == "completed" and page["next_cursor"] is None
    assert store.sweeps[second["sweep_id"]].state == COMPLETED


async def _start_without_running(store: MemoryStore, key: str) -> str:
    """Record a running sweep the way a dead process left it: accepted, never run."""
    _wire(store)
    with patch.object(sweep_handlers, "_start", lambda *a, **k: None):
        reply = await _start(3, key)
    record = store.sweeps[reply["sweep_id"]]
    assert record.state == RUNNING
    return str(reply["sweep_id"])


@pytest.mark.asyncio
async def test_a_sweep_this_process_runs_is_never_closed() -> None:
    store = MemoryStore()
    release = asyncio.Event()

    class _Slow(Converge):
        async def __call__(self, tenant: str, body: dict[str, Any]) -> dict[str, Any]:
            await release.wait()
            return await super().__call__(tenant, body)

    _wire(store, _Slow())
    reply = await _start(1, "k-live")
    await asyncio.sleep(0)
    store.stale.add(reply["sweep_id"])  # it looks stale, but its worker is here
    assert await sweep_handlers.close_stale_sweeps() == []
    release.set()
    await sweep_handlers.wait_for_background_sweeps()
    assert store.sweeps[reply["sweep_id"]].stats["enriched"] == 1


@pytest.mark.asyncio
async def test_a_closed_sweeps_late_worker_stops_before_reserving() -> None:
    store, converge = MemoryStore(), Converge()
    sweep_id = await _start_without_running(store, "k-late")
    store.stale.add(sweep_id)
    _wire(store, converge)
    assert await sweep_handlers.close_stale_sweeps() == [sweep_id]
    closed = store.sweeps[sweep_id]
    # The "dead" worker was only slow: it runs on with its old record.
    runner = sweep_handlers.SweepRunner(
        store=store,
        assess=Graph(),
        converge=converge,
        load_cartridge=sweep_handlers._load_cartridge,
        settings=get_settings(),
        clock=lambda: NOW,
    )
    await runner.run(replace(closed, state=RUNNING, skipped=(), stats={}))
    assert converge.bodies == []
    assert store.ledgers == {}


@pytest.mark.asyncio
async def test_a_failing_store_never_fails_the_sweep_action() -> None:
    store = MemoryStore()

    async def _down(*args: Any, **kwargs: Any) -> list[str]:
        raise ConnectionError("store down")

    store.close_stale_sweeps = _down  # type: ignore[method-assign]
    _wire(store)
    reply = await _start(1, "k-ok")
    await sweep_handlers.wait_for_background_sweeps()
    assert store.sweeps[reply["sweep_id"]].state == COMPLETED
    assert await sweep_handlers.close_stale_sweeps() == []


@pytest.mark.asyncio
async def test_the_poll_closes_stale_sweeps_with_no_request() -> None:
    store = MemoryStore()
    sweep_id = await _start_without_running(store, "k-poll")
    store.stale.add(sweep_id)
    assert sweep_handlers.start_stale_sweep_poll(interval_seconds=0.01)
    assert not sweep_handlers.start_stale_sweep_poll(interval_seconds=0.01)  # already runs
    try:
        for _ in range(100):
            if store.sweeps[sweep_id].state == COMPLETED:
                break
            await asyncio.sleep(0.01)
    finally:
        await sweep_handlers.stop_stale_sweep_poll()
    assert store.sweeps[sweep_id].state == COMPLETED
    await sweep_handlers.stop_stale_sweep_poll()  # stopping twice is harmless


def test_the_poll_starts_only_inside_a_running_loop() -> None:
    assert sweep_handlers.start_stale_sweep_poll() is False


@pytest.mark.asyncio
async def test_production_wiring_starts_the_poll_and_an_injected_store_does_not() -> None:
    try:
        sweep_handlers.init_sweep(reader=READER, store=MemoryStore())
        assert sweep_handlers._stale_poll is None
        sweep_handlers.init_sweep(reader=READER)
        assert sweep_handlers._stale_poll is not None
    finally:
        await sweep_handlers.stop_stale_sweep_poll()
        sweep_handlers.init_sweep(reader=READER, store=MemoryStore())


def test_the_stale_bound_is_far_above_a_live_sweeps_longest_gap() -> None:
    from app.engines.enrichment_sweep import ASSESS_BATCH_LIMIT
    from app.engines.handlers import DEFAULT_CONVERGE_TIMEOUT_SECONDS

    settings = get_settings()
    batches = -(-settings.l9_sweep_max_candidates_per_request // ASSESS_BATCH_LIMIT)
    readiness = batches * settings.default_timeout_seconds
    assert sweep_handlers.SWEEP_STALE_AFTER.total_seconds() > readiness
    assert sweep_handlers.SWEEP_STALE_AFTER.total_seconds() > 10 * DEFAULT_CONVERGE_TIMEOUT_SECONDS
