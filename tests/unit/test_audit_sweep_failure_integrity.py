"""Enrichment-sweep outcome, disposition and budget integrity (EIE-003).

L9_META:
  tier: 2
  domain: enrichment-sweep
  authority: docs/contracts/enrichment-sweep.md sections 3.4-6;
    docs/contracts/converge/response.v2.md section 6

Uses the established in-memory ledger and the real sweep handlers and runner
(tests/unit/test_enrichment_sweep.py). Only the converge reply is replaced, to
exercise replies the canonical path really produces (`failed`, `degraded`) and
replies it never should (no `state`, an unknown `state`).

What is asserted, and why:

* `stats.enriched` counts only `state == "completed"` replies. converge v2
  defines exactly three states (response.v2.md section 6, rule 4); `failed`
  and `degraded` are not enrichments, and a reply naming none of the three is
  not one either.
* Every candidate ends with exactly ONE disposition: a result row, or a skip
  entry. Never both, never neither. This is the invariant a naive fix breaks:
  `SweepRunner.run` derives "already handled" from `run.enriched`, so dropping
  a non-completed reply from `enriched` without recording it elsewhere makes a
  later runner crash re-mark that candidate `sweep_failed` while its result
  row stays stored.
* Spend is the reply's own `tokens_used` when usable, the full reservation
  otherwise (contract section 4), and no reservation is left in flight.

* A returned reply is stored as a result row, unchanged, whatever its state
  (contract section 3.4 step 5; owner decision 2026-11-26: not dropped, not
  turned into a skip). Only a raised converge is a skip.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest

from app.core.config import get_settings
from app.engines import sweep_handlers
from app.engines.enrichment_sweep import SweepRefusedError, SweepRunner, money
from tests.unit.test_enrichment_sweep import (
    _ENV,
    NOW,
    TENANT,
    Converge,
    Graph,
    MemoryStore,
    fetch_all,
    sweep,
    wire,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

# _ENV: MAX_BUDGET_TOKENS=1000 at TOKEN_RATE_USD_PER_1K=1.0, so one reservation is $1.00.
FULL_RESERVATION = Decimal("1.000000")


@pytest.fixture(autouse=True)
def _sweep_environment() -> Iterator[None]:
    with patch.dict(os.environ, _ENV):
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


def _cost(tokens: int) -> Decimal:
    return money(tokens * get_settings().token_rate_usd_per_1k / 1000)


class ScriptedConverge(Converge):
    """Returns the next scripted reply; an Exception instance is raised instead."""

    def __init__(self, *replies: dict[str, Any] | Exception):
        super().__init__()
        self.replies = list(replies)

    async def __call__(self, tenant: str, body: dict[str, Any]) -> dict[str, Any]:
        self.bodies.append(body)
        reply = self.replies[min(len(self.bodies), len(self.replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return {**reply, "entity_ref": body["entity_id"]}


def _completed(tokens: int = 100) -> dict[str, Any]:
    return {"state": "completed", "fields": {}, "proposals": [], "tokens_used": tokens}


def _dispositions(page: dict[str, Any]) -> dict[str, list[str]]:
    """entity_id -> every disposition the sweep recorded for it."""
    seen: dict[str, list[str]] = {}
    for row in page["results"]:
        seen.setdefault(row["entity_id"], []).append("result")
    for skip in page["skipped"]:
        seen.setdefault(skip["entity_id"], []).append(f"skip:{skip['reason']}")
    return seen


def _assert_one_disposition_each(page: dict[str, Any], n: int) -> dict[str, list[str]]:
    seen = _dispositions(page)
    expected = {f"rec-{i:04d}" for i in range(n)}
    assert set(seen) == expected, f"a candidate has no disposition: {seen}"
    doubled = {k: v for k, v in seen.items() if len(v) != 1}
    assert not doubled, f"a candidate has more than one disposition: {doubled}"
    return seen


@pytest.mark.parametrize("state", ["failed", "degraded"])
async def test_noncompleted_converge_reply_is_not_counted_enriched(state: str) -> None:
    """EIE-003/P1: a returned failure or deadline stop is not an enrichment."""
    store = MemoryStore()
    reply = {"state": state, "fields": {}, "proposals": [], "tokens_used": 250}
    if state == "failed":
        reply["failure_reason"] = "provider refused"
    converge = ScriptedConverge(reply)
    wire(store, Graph(), converge)

    result = await sweep(1, budget=2.0, key=f"noncompleted-{state}")
    page = await fetch_all(result["sweep_id"])
    ledger = store.ledgers[(TENANT, NOW.date())]

    assert len(converge.bodies) == 1
    assert page["stats"]["enriched"] == 0, page["stats"]
    seen = _assert_one_disposition_each(page, 1)
    assert seen == {"rec-0000": ["result"]}, "a returned reply is a result, never a skip"
    assert [row["response"] for row in page["results"]] == [{**reply, "entity_ref": "rec-0000"}]
    assert page["stats"][f"converge_state_{state}"] == 1
    assert ledger.spent_usd == _cost(250), "tokens_used is usable: charge the actual spend"
    assert ledger.reserved_usd == 0


@pytest.mark.parametrize(
    "reply",
    [
        {"fields": {}, "tokens_used": 100},
        {"state": "COMPLETED", "fields": {}, "tokens_used": 100},
        {"state": "success", "fields": {}, "tokens_used": 100},
        {"state": None, "fields": {}, "tokens_used": 100},
    ],
    ids=["missing-state", "wrong-case-state", "foreign-state", "null-state"],
)
async def test_reply_outside_the_three_converge_states_is_not_counted_enriched(
    reply: dict[str, Any],
) -> None:
    """Fail closed: only the literal `completed` is a success."""
    store = MemoryStore()
    wire(store, Graph(), ScriptedConverge(reply))

    result = await sweep(1, budget=2.0, key=f"malformed-{reply.get('state')!s}")
    page = await fetch_all(result["sweep_id"])
    ledger = store.ledgers[(TENANT, NOW.date())]

    assert page["stats"]["enriched"] == 0, page["stats"]
    seen = _assert_one_disposition_each(page, 1)
    assert seen == {"rec-0000": ["result"]}, "a returned reply is a result, never a skip"
    assert [row["response"] for row in page["results"]] == [{**reply, "entity_ref": "rec-0000"}]
    assert page["stats"]["converge_state_unrecognized"] == 1
    assert ledger.spent_usd == _cost(100)
    assert ledger.reserved_usd == 0


async def test_mixed_outcomes_count_exactly_the_completed_replies() -> None:
    """Counting is per reply, not per call: 2 of 4 completed means enriched == 2."""
    store = MemoryStore()
    replies = (
        _completed(100),
        {"state": "failed", "fields": {}, "proposals": [], "tokens_used": 50},
        TimeoutError("unknown provider spend"),
        _completed(200),
    )
    wire(store, Graph(), ScriptedConverge(*replies))

    result = await sweep(4, budget=10.0, key="mixed-outcomes")
    page = await fetch_all(result["sweep_id"])
    ledger = store.ledgers[(TENANT, NOW.date())]

    assert page["stats"]["enriched"] == 2, page["stats"]
    seen = _assert_one_disposition_each(page, 4)
    assert seen == {
        "rec-0000": ["result"],
        "rec-0001": ["result"],
        "rec-0002": ["skip:converge_failed"],
        "rec-0003": ["result"],
    }, "only a raised converge is skipped"
    assert page["stats"]["converge_state_failed"] == 1
    assert ledger.spent_usd == _cost(100) + _cost(50) + FULL_RESERVATION + _cost(200)
    assert ledger.reserved_usd == 0


async def test_runner_crash_after_a_noncompleted_reply_keeps_one_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression a naive EIE-003 fix introduces.

    `SweepRunner.run` marks every candidate not already handled as
    `sweep_failed` when the run crashes. A non-completed reply that is removed
    from `enriched` must still count as handled, or it gets a second
    disposition after its result row was already stored.
    """
    store = MemoryStore()
    wire(
        store,
        Graph(),
        ScriptedConverge({"state": "failed", "fields": {}, "proposals": [], "tokens_used": 10}),
    )
    original = SweepRunner._declared_targets
    calls = {"n": 0}

    def crash_on_second(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("unexpected runner error")
        return original(*args, **kwargs)

    monkeypatch.setattr(SweepRunner, "_declared_targets", staticmethod(crash_on_second))

    result = await sweep(3, budget=10.0, key="crash-after-failed")
    page = await fetch_all(result["sweep_id"])

    seen = _assert_one_disposition_each(page, 3)
    assert seen == {
        "rec-0000": ["result"],
        "rec-0001": ["skip:sweep_failed"],
        "rec-0002": ["skip:sweep_failed"],
    }
    assert page["results"][0]["response"]["state"] == "failed"
    assert page["stats"]["enriched"] == 0
    assert store.ledgers[(TENANT, NOW.date())].reserved_usd == 0


async def test_raised_converge_charges_full_reservation_and_keeps_no_success() -> None:
    """An exception is not a free enrichment; no reservation remains stranded."""
    store = MemoryStore()
    converge = ScriptedConverge(TimeoutError("unknown provider spend"))
    wire(store, Graph(), converge)

    result = await sweep(1, budget=2.0, key="raised-converge")
    page = await fetch_all(result["sweep_id"])
    ledger = store.ledgers[(TENANT, NOW.date())]

    assert len(converge.bodies) == 1
    assert page["results"] == []
    assert page["stats"]["enriched"] == 0
    assert page["skipped"] == [{"entity_id": "rec-0000", "reason": "converge_failed"}]
    assert ledger.spent_usd == FULL_RESERVATION
    assert ledger.reserved_usd == 0


async def test_other_tenant_cannot_page_known_sweep_or_touch_its_results() -> None:
    """Even with a known sweep id, a foreign tenant's read ends before results_page."""

    class ReadSpyStore(MemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.page_calls = 0

        async def results_page(self, sweep_id: str, after_seq: int, limit: int):
            self.page_calls += 1
            return await super().results_page(sweep_id, after_seq, limit)

    store = ReadSpyStore()
    wire(store, Graph(), Converge())
    result = await sweep(1, budget=2.0, key="tenant-isolation")
    assert len((await fetch_all(result["sweep_id"]))["results"]) == 1
    before = store.page_calls

    with pytest.raises(SweepRefusedError) as caught:
        await sweep_handlers.handle_enrichment_sweep_results(
            "other-tenant", {"sweep_id": result["sweep_id"]}
        )

    assert caught.value.code == "sweep_not_found"
    assert store.page_calls == before
