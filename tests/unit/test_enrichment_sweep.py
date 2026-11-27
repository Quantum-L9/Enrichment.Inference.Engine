"""Enrichment sweep: budget, readiness fail-closed, batching, results, flag.

docs/contracts/enrichment-sweep.md. The graph engine (through Gate), the
converge path and the database are replaced by in-process doubles; the
cartridge reader, request validation, ranking, budget arithmetic and the v2
converge resolution of the bodies the sweep builds are real.

The cartridge is the existing test-only fixture under tests/fixtures/cartridges.
Its tenant id, node labels and field names are read from it, never spelled here.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import UniqueConstraint

from app.core.config import get_settings
from app.engines import sweep_handlers
from app.engines.converge_request_v2 import resolve_converge_request_v2
from app.engines.domain_yaml_reader import DomainYamlReader
from app.engines.enrichment_sweep import (
    ASSESS_BATCH_LIMIT,
    SweepRefusedError,
    assessment_payload,
    converge_idempotency_key,
    effective_budget,
    parse_assessment,
    rank,
)
from app.engines.packet_router import NodeUnreachableError
from app.engines.spec_types import node_properties
from app.services.pg_models import (
    EnrichmentSweepRecord,
    EnrichmentSweepResult,
    SweepBudgetLedger,
)
from app.services.sweep_store import (
    COMPLETED,
    LedgerState,
    SweepRecord,
    SweepResultRow,
    interrupted_progress,
)

REPO = Path(__file__).resolve().parents[2]
CARTRIDGES = REPO / "tests/fixtures/cartridges"
TENANT = sorted(p.name for p in CARTRIDGES.iterdir() if (p / "spec.yaml").exists())[0]
READER = DomainYamlReader(CARTRIDGES)
CONFIG = READER.load(TENANT)
NODE = sorted(CONFIG.node_schemas)[0]
FIELDS = [p["name"] for p in node_properties(CONFIG.raw_spec, NODE) or []]
NOW = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)

# One converge reserves max_budget_tokens * rate / 1000 = $1.00.
_ENV = {
    "PERPLEXITY_API_KEY": "test-key",
    "API_SECRET_KEY": "test-secret-key-32-chars-long!!",
    "KB_DIR": "./kb",
    "L9_ENRICHMENT_SWEEP_ENABLED": "true",
    "MAX_BUDGET_TOKENS": "1000",
    "TOKEN_RATE_USD_PER_1K": "1.0",
    "L9_SWEEP_MAX_DAILY_BUDGET_USD": "100",
    "L9_SWEEP_MAX_CANDIDATES_PER_REQUEST": "500",
}


@pytest.fixture(autouse=True)
def _env() -> Iterator[None]:
    with patch.dict(os.environ, _ENV):
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


# ── Doubles ────────────────────────────────────────────────────────────────


class MemoryStore:
    """SweepStore with PostgreSQL's ledger semantics, held in memory."""

    def __init__(self) -> None:
        self.sweeps: dict[str, SweepRecord] = {}
        self.results: dict[str, list[SweepResultRow]] = {}
        self.ledgers: dict[tuple[str, date], LedgerState] = {}
        self.fail_ledger = False
        self.stale: set[str] = set()

    async def create_sweep(self, record: SweepRecord) -> tuple[SweepRecord, bool]:
        for existing in self.sweeps.values():
            if (existing.tenant_id, existing.idempotency_key) == (
                record.tenant_id,
                record.idempotency_key,
            ):
                return existing, False
        self.sweeps[record.sweep_id] = record
        return record, True

    async def get_sweep(self, tenant_id: str, sweep_id: str) -> SweepRecord | None:
        record = self.sweeps.get(sweep_id)
        return record if record is not None and record.tenant_id == tenant_id else None

    async def reserve(self, tenant_id: str, day: date, amount: Decimal, cap: Decimal) -> bool:
        if self.fail_ledger:
            raise ConnectionError("ledger down")
        state = self.ledgers.get((tenant_id, day), LedgerState(Decimal(0), Decimal(0)))
        if state.spent_usd + state.reserved_usd + amount > cap:
            return False
        self.ledgers[(tenant_id, day)] = LedgerState(state.spent_usd, state.reserved_usd + amount)
        return True

    async def settle(self, tenant_id: str, day: date, reserved: Decimal, actual: Decimal) -> None:
        state = self.ledgers[(tenant_id, day)]
        self.ledgers[(tenant_id, day)] = LedgerState(
            state.spent_usd + actual, state.reserved_usd - reserved
        )

    async def ledger(self, tenant_id: str, day: date) -> LedgerState:
        return self.ledgers.get((tenant_id, day), LedgerState(Decimal(0), Decimal(0)))

    async def add_result(
        self, sweep_id: str, seq: int, entity_id: str, response: dict[str, Any], cost: Decimal
    ) -> None:
        self.results.setdefault(sweep_id, []).append(SweepResultRow(seq, entity_id, response))

    async def save_progress(
        self,
        sweep_id: str,
        skipped: Any,
        stats: dict[str, Any],
        *,
        completed_at: datetime | None = None,
    ) -> None:
        record = self.sweeps[sweep_id]
        changes: dict[str, Any] = {"skipped": tuple(skipped), "stats": dict(stats)}
        if completed_at is not None:
            changes.update(state=COMPLETED, completed_at=completed_at)
        self.sweeps[sweep_id] = replace(record, **changes)

    async def results_page(self, sweep_id: str, after_seq: int, limit: int) -> list[SweepResultRow]:
        rows = [r for r in self.results.get(sweep_id, []) if r.seq > after_seq]
        return sorted(rows, key=lambda r: r.seq)[:limit]

    async def purge_expired(self, now: datetime, ttl: timedelta) -> int:
        expired = [
            k
            for k, r in self.sweeps.items()
            if r.completed_at is not None and r.completed_at < now - ttl
        ]
        for key in expired:
            del self.sweeps[key]
            self.results.pop(key, None)
        return len(expired)

    async def close_stale_sweeps(
        self, now: datetime, stale_after: timedelta, exclude: Any = ()
    ) -> list[str]:
        """Closes the running sweeps a test marked stale (`self.stale`)."""
        closed = []
        for sweep_id in sorted(self.stale - set(exclude)):
            record = self.sweeps.get(sweep_id)
            if record is None or record.state == COMPLETED:
                continue
            rows = self.results.get(sweep_id, [])
            handled = {r.entity_id for r in rows}
            skipped, stats = interrupted_progress(record, handled, [r.response for r in rows])
            self.sweeps[sweep_id] = replace(
                record, skipped=tuple(skipped), stats=stats, state=COMPLETED, completed_at=now
            )
            closed.append(sweep_id)
        return closed


def target(name: str, priority: float = 50.0) -> dict[str, Any]:
    """CEG's EnrichmentTarget, as engine/health/field_health.py serialises it."""
    return {
        "field_name": name,
        "priority_score": priority,
        "is_gate_critical": False,
        "unlocks_rules": [],
        "estimated_tokens": 0,
    }


def healthy(entity_id: str, *, gaps: list[str], targets: list[str], score: float) -> dict[str, Any]:
    """One EntityHealthResponse in CEG's real shape: gaps are names, targets objects."""
    return {
        "entity_id": entity_id,
        "entity_health": {
            "critical_gaps": gaps,
            "enrichment_targets": [target(name) for name in targets],
            "readiness_score": score,
            "grade": "C",
            "gate_completeness": 0.5,
            # Fields the sweep must ignore.
            "enrichment_priority": 99,
            "enrichment_dispatch": {"x": 1},
        },
        "upgrade_prompt": "ignored",
    }


class Graph:
    """CEG health_batch_assess double: every candidate has one critical gap."""

    def __init__(self, reply: Any = None, *, raises: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reply = reply
        self.raises = raises

    async def __call__(self, tenant: str, payload: dict[str, Any], key: str) -> dict[str, Any]:
        self.calls.append(payload)
        if self.raises is not None:
            raise self.raises
        if self.reply is not None:
            return self.reply(payload) if callable(self.reply) else self.reply
        # CEG's BatchHealthReport has no `status` field: a successful batch
        # answer carries none (only the disabled API answers {"status": "disabled"}).
        return {
            "entities": [
                healthy(e["entity_id"], gaps=[FIELDS[-1]], targets=[], score=0.4)
                for e in payload["entities"]
            ],
        }


class Converge:
    """Canonical converge double: answers a v2-shaped body and records the request."""

    def __init__(self, tokens: int = 1000) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.tokens = tokens

    async def __call__(self, tenant: str, body: dict[str, Any]) -> dict[str, Any]:
        self.bodies.append(body)
        return {
            "state": "completed",
            "contract_version": "eie.converge.response/v2",
            "fields": {},
            "field_evidence": {t["field"]: {"status": "unresolved"} for t in body["targets"]},
            "proposals": [],
            "tokens_used": self.tokens,
            "entity_ref": body["entity_id"],
        }


def candidates(n: int) -> list[dict[str, Any]]:
    return [
        {"entity_id": f"rec-{i:04d}", "node_label": NODE, "entity": {"id": f"rec-{i:04d}"}}
        for i in range(n)
    ]


def wire(store: MemoryStore, graph: Graph, converge: Converge) -> None:
    sweep_handlers.init_sweep(
        reader=READER, store=store, assess=graph, converge=converge, clock=lambda: NOW
    )


async def sweep(n: int, budget: float, key: str) -> dict[str, Any]:
    reply = await sweep_handlers.handle_enrichment_sweep(
        TENANT, {"candidates": candidates(n), "daily_budget_usd": budget, "idempotency_key": key}
    )
    await sweep_handlers.wait_for_background_sweeps()
    return reply


async def fetch_all(sweep_id: str) -> dict[str, Any]:
    page = await sweep_handlers.handle_enrichment_sweep_results(TENANT, {"sweep_id": sweep_id})
    results = list(page["results"])
    while page["next_cursor"] is not None:
        page = await sweep_handlers.handle_enrichment_sweep_results(
            TENANT, {"sweep_id": sweep_id, "cursor": page["next_cursor"]}
        )
        results.extend(page["results"])
    return {**page, "results": results}


# ── Budget ─────────────────────────────────────────────────────────────────


async def test_budget_stops_at_cap_and_ledger_persists_across_calls_in_one_day() -> None:
    store, converge = MemoryStore(), Converge(tokens=1000)  # $1.00 each
    wire(store, Graph(), converge)

    first = await sweep(5, budget=2.5, key="day-1-a")
    assert len(converge.bodies) == 2
    page = await fetch_all(first["sweep_id"])
    assert page["budget"] == {"effective_usd": 2.5, "spent_usd": 2.0, "remaining_usd": 0.5}
    assert [s["reason"] for s in page["skipped"]].count("budget_exhausted") == 3

    second = await sweep(5, budget=2.5, key="day-1-b")
    assert len(converge.bodies) == 2, "the day's ledger, not the request, bounds the second sweep"
    page = await fetch_all(second["sweep_id"])
    assert page["results"] == []
    assert {s["reason"] for s in page["skipped"]} == {"budget_exhausted"}


async def test_effective_budget_is_min_of_request_and_deployment_ceiling() -> None:
    assert effective_budget(Decimal("10"), 1.5) == Decimal("1.500000")
    assert effective_budget(Decimal("1.2"), 5.0) == Decimal("1.200000")

    with patch.dict(os.environ, {"L9_SWEEP_MAX_DAILY_BUDGET_USD": "1.5"}):
        get_settings.cache_clear()
        store, converge = MemoryStore(), Converge()
        wire(store, Graph(), converge)
        reply = await sweep(4, budget=10.0, key="ceiling")
        assert len(converge.bodies) == 1
        page = await fetch_all(reply["sweep_id"])
        assert page["budget"]["effective_usd"] == 1.5


async def test_ledger_unavailable_enriches_nothing() -> None:
    store, converge = MemoryStore(), Converge()
    store.fail_ledger = True
    wire(store, Graph(), converge)
    reply = await sweep(3, budget=50, key="ledger-down")
    assert converge.bodies == []
    page = await fetch_all(reply["sweep_id"])
    assert {s["reason"] for s in page["skipped"]} == {"budget_ledger_unavailable"}


async def test_actual_cost_is_recorded_after_each_enrichment() -> None:
    store, converge = MemoryStore(), Converge(tokens=250)  # reserve $1, spend $0.25
    wire(store, Graph(), converge)
    reply = await sweep(2, budget=50, key="actual")
    page = await fetch_all(reply["sweep_id"])
    assert page["budget"]["spent_usd"] == 0.5
    assert store.ledgers[(TENANT, NOW.date())].reserved_usd == 0


# ── Readiness fail-closed ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("graph", "reason"),
    [
        (Graph({"status": "disabled"}), "readiness_disabled"),
        (Graph({"status": "refused", "entities": []}), "readiness_refused"),
        (Graph(raises=NodeUnreachableError("gate down")), "readiness_failed"),
        (Graph({"status": "success", "entities": []}), "readiness_absent"),
        (Graph({"status": "success", "entities": "nope"}), "readiness_malformed"),
    ],
    ids=["disabled", "refused", "failed", "absent", "malformed"],
)
async def test_unassessed_candidates_are_never_enriched(graph: Graph, reason: str) -> None:
    store, converge = MemoryStore(), Converge()
    wire(store, graph, converge)
    reply = await sweep(3, budget=50, key=f"readiness-{reason}")
    assert converge.bodies == []
    page = await fetch_all(reply["sweep_id"])
    assert {s["reason"] for s in page["skipped"]} == {reason}


async def test_candidate_absent_from_reply_is_not_enriched_while_others_are() -> None:
    def reply(payload: dict[str, Any]) -> dict[str, Any]:
        present = payload["entities"][1:]  # CEG truncated the first one
        return {
            "status": "success",
            "total_truncated": 1,
            "entities": [
                healthy(e["entity_id"], gaps=[FIELDS[-1]], targets=[], score=0.3) for e in present
            ],
        }

    store, converge = MemoryStore(), Converge()
    wire(store, Graph(reply), converge)
    out = await sweep(3, budget=50, key="partial")
    assert sorted(b["entity_id"] for b in converge.bodies) == ["rec-0001", "rec-0002"]
    page = await fetch_all(out["sweep_id"])
    assert page["skipped"] == [{"entity_id": "rec-0000", "reason": "readiness_absent"}]


async def test_candidates_without_gaps_are_not_enriched() -> None:
    def reply(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "entities": [
                healthy(e["entity_id"], gaps=[], targets=[], score=0.9) for e in payload["entities"]
            ]
        }

    store, converge = MemoryStore(), Converge()
    wire(store, Graph(reply), converge)
    out = await sweep(2, budget=50, key="no-gaps")
    assert converge.bodies == []
    assert {s["reason"] for s in (await fetch_all(out["sweep_id"]))["skipped"]} == {
        "no_readiness_gaps"
    }


# ── The readiness request ──────────────────────────────────────────────────


async def test_batch_payload_uses_data_and_omits_domain_and_tier() -> None:
    graph = Graph()
    wire(MemoryStore(), graph, Converge())
    await sweep(2, budget=50, key="payload")
    payload = graph.calls[0]
    assert payload["subaction"] == "health_batch_assess"
    assert payload["max_entities"] == 2
    assert "domain" not in payload and "tier" not in payload
    for entity in payload["entities"]:
        assert set(entity) == {"entity_id", "node_label", "data", "field_confidences"}
        assert "entity_data" not in entity
        assert entity["data"] == {"id": entity["entity_id"]}


async def test_candidates_beyond_the_batch_limit_are_split() -> None:
    graph = Graph()
    wire(MemoryStore(), graph, Converge())
    await sweep(2 * ASSESS_BATCH_LIMIT + 5, budget=0.5, key="batches")
    sizes = [len(call["entities"]) for call in graph.calls]
    assert sizes == [ASSESS_BATCH_LIMIT, ASSESS_BATCH_LIMIT, 5]
    assert all(call["max_entities"] <= ASSESS_BATCH_LIMIT for call in graph.calls)
    with pytest.raises(ValueError, match="at most"):
        assessment_payload(candidates(ASSESS_BATCH_LIMIT + 1))


def test_only_the_named_readiness_fields_are_read() -> None:
    batch = candidates(1)
    found, skipped = parse_assessment(
        {"entities": [healthy("rec-0000", gaps=["a"], targets=["b"], score=0.2)]}, batch, 0
    )
    assert skipped == []
    readiness = found["rec-0000"]
    assert (readiness.critical_gaps, readiness.enrichment_targets) == (("a",), ("b",))
    assert not hasattr(readiness, "enrichment_priority")


def test_ranking_puts_critical_gaps_first_then_ceg_order_then_lower_score() -> None:
    reply = {
        "entities": [
            healthy("a", gaps=[], targets=["x"], score=0.1),
            healthy("b", gaps=["x"], targets=[], score=0.9),
            healthy("c", gaps=["x"], targets=[], score=0.2),
        ]
    }
    batch = [{"entity_id": e} for e in "abc"]
    found, _ = parse_assessment(reply, batch, 0)
    assert [r.entity_id for r in rank(list(found.values()))] == ["b", "c", "a"]

    other_batch, _ = parse_assessment(
        {"entities": [healthy("d", gaps=["x"], targets=[], score=0.5)]}, [{"entity_id": "d"}], 1
    )
    ranked = rank([*found.values(), *other_batch.values()])
    # b and d share CEG position 0 in their batches; the lower score goes first.
    assert [r.entity_id for r in ranked] == ["d", "b", "c", "a"]


# ── Converge requests and results ──────────────────────────────────────────


async def test_converge_gets_a_resolvable_v2_request_and_undeclared_targets_are_dropped() -> None:
    def reply(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "entities": [
                healthy(
                    e["entity_id"],
                    gaps=[FIELDS[-1], "not_a_declared_field"],
                    targets=[FIELDS[-2], FIELDS[-1]],
                    score=0.4,
                )
                for e in payload["entities"]
            ]
        }

    converge = Converge()
    wire(MemoryStore(), Graph(reply), converge)
    out = await sweep(1, budget=50, key="v2")
    body = converge.bodies[0]
    assert [t["field"] for t in body["targets"]] == [FIELDS[-1], FIELDS[-2]]
    assert body["idempotency_key"] == converge_idempotency_key(out["sweep_id"], "rec-0000")
    resolved = resolve_converge_request_v2(TENANT, body, READER)
    assert resolved is not None and resolved.node == NODE
    page = await fetch_all(out["sweep_id"])
    assert page["stats"]["targets_dropped_undeclared"] == 1


async def test_results_expose_the_converge_body_unchanged() -> None:
    converge = Converge()
    wire(MemoryStore(), Graph(), converge)
    out = await sweep(2, budget=50, key="bodies")
    page = await fetch_all(out["sweep_id"])
    assert page["state"] == "completed"
    expected = [await Converge()(TENANT, body) for body in converge.bodies]
    assert [r["response"] for r in page["results"]] == expected
    assert [r["entity_id"] for r in page["results"]] == [b["entity_id"] for b in converge.bodies]


async def test_same_idempotency_key_returns_the_same_sweep_without_rerunning() -> None:
    converge = Converge()
    wire(MemoryStore(), Graph(), converge)
    first = await sweep(2, budget=50, key="same")
    second = await sweep(2, budget=50, key="same")
    assert first["sweep_id"] == second["sweep_id"]
    assert len(converge.bodies) == 2


async def test_invalid_candidates_are_rejected_and_the_cap_applies() -> None:
    with patch.dict(os.environ, {"L9_SWEEP_MAX_CANDIDATES_PER_REQUEST": "2"}):
        get_settings.cache_clear()
        wire(MemoryStore(), Graph(), Converge())
        rows = [*candidates(3), {"entity_id": "bad", "entity": "x"}]
        rows.append({"entity_id": "rec-0000", "node_label": "UndeclaredNode", "entity": {}})
        reply = await sweep_handlers.handle_enrichment_sweep(
            TENANT, {"candidates": rows, "daily_budget_usd": 5, "idempotency_key": "caps"}
        )
        await sweep_handlers.wait_for_background_sweeps()
    assert reply["accepted"] == ["rec-0000", "rec-0001"]
    assert {r["reason"] for r in reply["rejected"]} == {"candidate_cap_exceeded"}


async def test_sweeps_expire_ttl_after_completion_and_are_deleted_by_the_sweep_action() -> None:
    store = MemoryStore()
    wire(store, Graph(), Converge())
    out = await sweep(1, budget=50, key="ttl")
    later = NOW + timedelta(hours=get_settings().l9_sweep_result_ttl_hours + 1)
    sweep_handlers.init_sweep(
        reader=READER, store=store, assess=Graph(), converge=Converge(), clock=lambda: later
    )
    with pytest.raises(SweepRefusedError) as refused:
        await sweep_handlers.handle_enrichment_sweep_results(TENANT, {"sweep_id": out["sweep_id"]})
    assert refused.value.code == "sweep_not_found"
    assert out["sweep_id"] in store.sweeps, "the read-only results action deletes nothing"
    await sweep(1, budget=50, key="later")
    assert out["sweep_id"] not in store.sweeps


# ── Idempotency and read-only results (CTO directive) ──────────────────────


def _snapshot(store: MemoryStore) -> tuple[Any, ...]:
    return (
        dict(store.sweeps),
        {k: list(v) for k, v in store.results.items()},
        dict(store.ledgers),
    )


async def test_sweep_replay_returns_the_original_answer_without_new_work_or_budget() -> None:
    store, graph, converge = MemoryStore(), Graph(), Converge()
    wire(store, graph, converge)
    rows = [*candidates(2), {"entity_id": "bad", "entity": "x"}]
    body = {"candidates": rows, "daily_budget_usd": 50, "idempotency_key": "replay"}
    first = await sweep_handlers.handle_enrichment_sweep(TENANT, body)
    await sweep_handlers.wait_for_background_sweeps()
    before = (_snapshot(store), len(graph.calls), len(converge.bodies))

    changed = {"candidates": candidates(5), "daily_budget_usd": 1, "idempotency_key": "replay"}
    for replay in (body, changed):
        again = await sweep_handlers.handle_enrichment_sweep(TENANT, replay)
        assert sweep_handlers._background == set(), "a replay starts no work"
        await sweep_handlers.wait_for_background_sweeps()
        assert again == first
    assert (_snapshot(store), len(graph.calls), len(converge.bodies)) == before


async def test_sweep_replay_with_nothing_accepted_keeps_its_original_answer() -> None:
    store = MemoryStore()
    wire(store, Graph(), Converge())
    body = {"candidates": [{"entity_id": "bad"}], "daily_budget_usd": 5, "idempotency_key": "none"}
    first = await sweep_handlers.handle_enrichment_sweep(TENANT, body)
    assert first["accepted"] == [] and first["rejected"]
    other = {"candidates": candidates(1), "daily_budget_usd": 5, "idempotency_key": "none"}
    assert await sweep_handlers.handle_enrichment_sweep(TENANT, other) == first
    assert sweep_handlers._background == set()


async def test_results_are_read_only_and_a_replayed_page_is_the_same_page() -> None:
    store = MemoryStore()
    wire(store, Graph(), Converge())
    with patch.dict(os.environ, {"L9_SWEEP_RESULTS_PAGE_SIZE": "2"}):
        get_settings.cache_clear()
        out = await sweep(3, budget=50, key="pages")
        before = _snapshot(store)
        request = {"sweep_id": out["sweep_id"]}
        first = await sweep_handlers.handle_enrichment_sweep_results(TENANT, request)
        assert await sweep_handlers.handle_enrichment_sweep_results(TENANT, request) == first
        cursor = {"sweep_id": out["sweep_id"], "cursor": first["next_cursor"]}
        second = await sweep_handlers.handle_enrichment_sweep_results(TENANT, cursor)
        assert await sweep_handlers.handle_enrichment_sweep_results(TENANT, cursor) == second
    assert len(first["results"]) == 2 and len(second["results"]) == 1
    assert second["next_cursor"] is None
    assert _snapshot(store) == before
    assert sweep_handlers._background == set()


async def test_results_of_a_running_sweep_never_start_or_resume_it() -> None:
    store, converge = MemoryStore(), Converge()
    wire(store, Graph(), converge)
    record = SweepRecord(
        sweep_id="s" * 32,
        tenant_id=TENANT,
        idempotency_key="interrupted",
        day=NOW.date(),
        effective_budget_usd=Decimal("50"),
        candidates=tuple(candidates(2)),
    )
    await store.create_sweep(record)
    page = await sweep_handlers.handle_enrichment_sweep_results(TENANT, {"sweep_id": "s" * 32})
    assert page["state"] == "running" and page["results"] == []
    assert page["next_cursor"] == "0"
    assert sweep_handlers._background == set() and converge.bodies == []
    with pytest.raises(SweepRefusedError) as refused:
        await sweep_handlers.handle_enrichment_sweep_results(
            TENANT, {"sweep_id": "s" * 32, "idempotency_key": "k"}
        )
    assert refused.value.code == "invalid_request"


# ── Flag ───────────────────────────────────────────────────────────────────


async def test_flag_off_refuses_both_actions() -> None:
    converge, graph = Converge(), Graph()
    wire(MemoryStore(), graph, converge)
    with patch.dict(os.environ, {"L9_ENRICHMENT_SWEEP_ENABLED": "false"}):
        get_settings.cache_clear()
        assert get_settings().l9_enrichment_sweep_enabled is False
        with pytest.raises(SweepRefusedError) as sweep_refused:
            await sweep_handlers.handle_enrichment_sweep(
                TENANT,
                {"candidates": candidates(1), "daily_budget_usd": 1, "idempotency_key": "off"},
            )
        with pytest.raises(SweepRefusedError) as results_refused:
            await sweep_handlers.handle_enrichment_sweep_results(TENANT, {"sweep_id": "x"})
    assert sweep_refused.value.code == results_refused.value.code == "disabled"
    assert graph.calls == [] and converge.bodies == []


def test_flag_is_off_by_default() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("L9_ENRICHMENT_SWEEP_ENABLED", None)
        get_settings.cache_clear()
        assert get_settings().l9_enrichment_sweep_enabled is False


def test_both_actions_are_registered_in_lockstep() -> None:
    from app.engines.handlers import get_handler_map
    from app.main import ADVERTISED_ACTIONS, RUNTIME_ALLOWED_ACTIONS

    handlers = get_handler_map()
    for action in ("enrichment-sweep", "enrichment-sweep-results"):
        assert action in ADVERTISED_ACTIONS
        assert action in RUNTIME_ALLOWED_ACTIONS
        assert action in handlers
    # advertised ⊆ implemented ⊆ runtime_allowed
    assert set(ADVERTISED_ACTIONS) <= set(RUNTIME_ALLOWED_ACTIONS)


# ── Persistence shape ──────────────────────────────────────────────────────


def test_ledger_is_one_row_per_tenant_per_day_and_sweeps_are_unique_per_key() -> None:
    assert [c.name for c in SweepBudgetLedger.__table__.primary_key.columns] == [
        "tenant_id",
        "day",
    ]
    uniques = {
        tuple(sorted(col.name for col in c.columns))
        for c in EnrichmentSweepRecord.__table__.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert ("idempotency_key", "tenant_id") in uniques
    result_uniques = {
        tuple(sorted(col.name for col in c.columns))
        for c in EnrichmentSweepResult.__table__.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert ("entity_id", "sweep_id") in result_uniques


def test_migration_creates_the_sweep_tables_after_003() -> None:
    source = (REPO / "migrations/versions/004_enrichment_sweep.py").read_text()
    assert re.search(r'^down_revision = "003"$', source, re.M)
    for table in ("sweep_budget_ledger", "enrichment_sweeps", "enrichment_sweep_results"):
        assert f'"{table}"' in source


# ── Domain-agnostic guard ──────────────────────────────────────────────────

_NEW_MODULES = (
    "app/engines/enrichment_sweep.py",
    "app/engines/sweep_handlers.py",
    "app/services/sweep_store.py",
    "migrations/versions/004_enrichment_sweep.py",
)


def _cartridge_vocabulary() -> set[str]:
    """Every tenant id and node label any cartridge in the repository declares."""
    words: set[str] = set()
    for root in (REPO / "domains", CARTRIDGES):
        reader = DomainYamlReader(root)
        for path in sorted(root.iterdir()):
            if not (path / "spec.yaml").exists():
                continue
            words.add(path.name)
            try:
                words.update(reader.load(path.name).node_schemas)
            except Exception:  # an unloadable cartridge still names its tenant
                continue
    return {w for w in words if len(w) > 2}


def test_new_modules_name_no_tenant_domain_or_node() -> None:
    vocabulary = _cartridge_vocabulary()
    assert TENANT in vocabulary and NODE in vocabulary
    for module in _NEW_MODULES:
        text = (REPO / module).read_text()
        hits = sorted(w for w in vocabulary if re.search(rf"\b{re.escape(w)}\b", text))
        assert hits == [], f"{module} names cartridge vocabulary {hits}"


def test_ceg_shapes_parse_and_a_wrong_target_shape_is_malformed() -> None:
    """Targets are EnrichmentTarget objects keyed `field_name`; gaps are names.

    A target object without `field_name` (e.g. keyed `field`) is not CEG's shape:
    the candidate is skipped as malformed, never enriched on a guessed field.
    """
    batch = candidates(2)
    ok = healthy("rec-0000", gaps=["a"], targets=["b", "c"], score=0.2)
    wrong = healthy("rec-0001", gaps=[], targets=[], score=0.2)
    wrong["entity_health"]["enrichment_targets"] = [{"field": "b"}]
    found, skipped = parse_assessment({"entities": [ok, wrong]}, batch, 0)
    assert found["rec-0000"].critical_gaps == ("a",)
    assert found["rec-0000"].enrichment_targets == ("b", "c")
    assert "rec-0001" not in found
    assert [s["entity_id"] for s in skipped] == ["rec-0001"]
