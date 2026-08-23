"""Tests for app/engines/convergence/loop_state.py

Covers: LoopState state machine, LoopStateStore persistence, LoopStatus enum.
"""

from __future__ import annotations

import pytest

from app.engines.convergence.loop_state import (
    LoopState,
    LoopStateStore,
    LoopStatus,
)
from app.models.loop_schemas import CostSummary, PassResult


class InMemoryLoopStateStore(LoopStateStore):
    """Dict-backed store for unit tests — no Redis/Postgres."""

    def __init__(self) -> None:
        self._states: dict[str, LoopState] = {}

    async def save(self, state: LoopState) -> None:
        state.touch()
        self._states[state.run_id] = state.model_copy(deep=True)

    async def load(self, run_id: str) -> LoopState | None:
        state = self._states.get(run_id)
        return state.model_copy(deep=True) if state is not None else None

    async def list_active(self, domain: str | None = None) -> list[LoopState]:
        active: list[LoopState] = []
        for state in self._states.values():
            if state.status != LoopStatus.RUNNING:
                continue
            if domain and state.domain != domain:
                continue
            active.append(state.model_copy(deep=True))
        return active


class TestLoopStatus:
    """Tests for LoopStatus enum."""

    def test_all_statuses_present(self):
        expected = {
            "running",
            "converged",
            "max_passes",
            "budget_exhausted",
            "human_hold",
            "diminishing_returns",
            "failed",
        }
        actual = {s.value for s in LoopStatus}
        assert expected.issubset(actual)


class TestLoopState:
    """Tests for convergence loop state machine."""

    def test_init_generates_run_id(self):
        state = LoopState(entity_id="test-1", domain="plastics_recycling")
        assert state.run_id is not None
        assert len(state.run_id) > 0

    def test_initial_status_is_running(self):
        state = LoopState(entity_id="test-1", domain="plastics_recycling")
        assert state.status == LoopStatus.RUNNING

    def test_current_pass_starts_at_zero(self):
        state = LoopState(entity_id="test-1", domain="plastics_recycling")
        assert state.current_pass == 0

    def test_accumulated_fields_empty_initially(self):
        state = LoopState(entity_id="test-1", domain="plastics_recycling")
        assert len(state.accumulated_fields) == 0

    def test_passes_completed_list_empty(self):
        state = LoopState(entity_id="test-1", domain="plastics_recycling")
        assert state.passes_completed == []

    def test_append_pass_result(self):
        state = LoopState(entity_id="test-1", domain="plastics_recycling")
        pr = PassResult(
            pass_number=1,
            fields_enriched=["polymer_type"],
            tokens_used=1200,
            duration_ms=3000,
        )
        state.passes_completed.append(pr)
        state.current_pass = 1
        assert len(state.passes_completed) == 1
        assert state.current_pass == 1

    def test_cost_summary_tracking(self):
        state = LoopState(entity_id="test-1", domain="plastics_recycling")
        state.cost_summary = CostSummary(total_tokens=2400, total_cost_usd=0.012)
        assert state.cost_summary.total_tokens == 2400

    def test_timestamps_auto_set(self):
        state = LoopState(entity_id="test-1", domain="plastics_recycling")
        assert state.created_at is not None
        assert state.updated_at is not None


class TestLoopStateStore:
    """Tests for in-memory state persistence."""

    @pytest.fixture
    def store(self) -> InMemoryLoopStateStore:
        return InMemoryLoopStateStore()

    @pytest.mark.asyncio
    async def test_save_and_load(self, store: InMemoryLoopStateStore):
        state = LoopState(entity_id="test-1", domain="plastics_recycling")
        await store.save(state)
        loaded = await store.load(state.run_id)
        assert loaded is not None
        assert loaded.entity_id == "test-1"

    @pytest.mark.asyncio
    async def test_load_nonexistent_returns_none(self, store: InMemoryLoopStateStore):
        loaded = await store.load("nonexistent-id")
        assert loaded is None

    @pytest.mark.asyncio
    async def test_list_active(self, store: InMemoryLoopStateStore):
        s1 = LoopState(entity_id="a", domain="plastics_recycling", status=LoopStatus.RUNNING)
        s2 = LoopState(entity_id="b", domain="plastics_recycling", status=LoopStatus.CONVERGED)
        await store.save(s1)
        await store.save(s2)
        active = await store.list_active(domain="plastics_recycling")
        assert any(s.entity_id == "a" for s in active)
        assert all(s.status == LoopStatus.RUNNING for s in active)
