"""`state="completed"` may not outrun the durable write it implies.

The failure this pins was quiet by construction. `SideEffectCoordinator` caught
every persistence exception, appended it to a report, logged a warning, and
returned normally; `handlers._persist_and_sync` discarded the report; the
handler had already built a `completed` response. So a convergence whose result
never reached PostgreSQL was acknowledged to Odoo as durable, the semantic key
was marked complete — retiring the retry that would have fixed it — and the
only trace was a log line nobody was reading.

Three separate assertions are needed, because any one of them can hold while
the system is still broken:

* the response is not `completed`;
* no completion marker is recorded, so a retry is still possible;
* optional effects failing does NOT produce the same outcome — otherwise the
  fix would just be a blunter failure mode rather than a semantic distinction.

L9_META:
  tier: 2
  domain: convergence
  authority: L9 Master Kernel v3.0
  pr_class: app_code + tier2_test
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.engines.handlers import handle_converge
from app.models.schemas import EnrichResponse
from app.services.side_effect_coordinator import (
    RequiredSideEffectError,
    SideEffectCoordinator,
    get_side_effect_coordinator,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

IDEM_KEY = "converge:corr-abc"


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "entity": {
            "name": "Acme Recycling",
            "id": "res.partner:55",
            "_odoo_entity_id": "res.partner:55",
        },
        "object_type": "plasticos",
        "objective": "Full entity enrichment and inference",
        "max_variations": 5,
        "kb_context": "plasticos",
        "idempotency_key": IDEM_KEY,
    }
    payload.update(overrides)
    return payload


def _converged() -> EnrichResponse:
    return EnrichResponse(
        fields={"website": "https://acme.example"},
        confidence=0.91,
        pass_count=2,
        state="completed",
    )


@pytest.fixture
def converge_runtime():
    """Canonical converge with every egress stubbed; persistence is the variable."""
    with (
        patch("app.engines.handlers.run_convergence_loop", new_callable=AsyncMock) as loop,
        patch(
            "app.services.result_store.ResultStore.persist_enrich_response",
            new_callable=AsyncMock,
        ) as persist,
        patch("app.engines.packet_router.get_router") as router_factory,
        patch("app.services.event_emitter.get_emitter") as emitter_factory,
    ):
        loop.return_value = _converged()
        router = router_factory.return_value
        router.notify_graph_sync = AsyncMock(return_value=None)
        router.notify_score_invalidate = AsyncMock(return_value=None)
        emitter_factory.return_value.emit_enrichment_completed = AsyncMock(return_value=None)

        get_side_effect_coordinator().reset_for_tests()
        yield {"loop": loop, "persist": persist, "router": router}
        get_side_effect_coordinator().reset_for_tests()


async def test_persistence_success_still_returns_completed(converge_runtime) -> None:
    """The control: nothing about the happy path changed."""
    result = await handle_converge("acme", _payload())

    assert result["state"] == "completed"
    assert converge_runtime["persist"].await_count == 1


async def test_failed_required_persistence_does_not_return_completed(converge_runtime) -> None:
    """ADR-EIE-006 — durable before completed."""
    converge_runtime["persist"].side_effect = RuntimeError("connection refused")

    result = await handle_converge("acme", _payload())

    assert result["state"] != "completed"
    assert result["state"] == "failed"
    assert "persistence" in result["failure_reason"]


async def test_failed_required_persistence_records_no_completion_marker(
    converge_runtime,
) -> None:
    """The retry must survive the failure.

    Marking the key complete on a failed write is worse than the failure
    itself: the operation is then permanently un-retryable in this process,
    so the very next attempt is skipped as a duplicate of something that
    never happened.
    """
    converge_runtime["persist"].side_effect = RuntimeError("connection refused")
    coordinator = get_side_effect_coordinator()

    await handle_converge("acme", _payload())

    assert coordinator._completed == set()

    # And the retry genuinely runs: persistence is attempted a second time.
    converge_runtime["persist"].side_effect = None
    result = await handle_converge("acme", _payload())

    assert result["state"] == "completed"
    assert converge_runtime["persist"].await_count == 2


async def test_optional_effect_failure_does_not_falsify_a_completed_result(
    converge_runtime,
) -> None:
    """ADR-EIE-012 — required and optional are different contracts.

    Score invalidation failing does not make the enrichment the caller is
    holding wrong, so it must not turn a durable result into a failure. If this
    ever reported `failed`, the required/optional distinction would have
    collapsed in the other direction.
    """
    converge_runtime["router"].notify_score_invalidate.side_effect = RuntimeError("gate down")

    result = await handle_converge("acme", _payload())

    assert result["state"] == "completed"
    assert converge_runtime["persist"].await_count == 1


# ── Coordinator-level semantics ────────────────────────────────────────────


async def _commit(coordinator: SideEffectCoordinator, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "tenant": "acme",
        "entity_id": "res.partner:55",
        "object_type": "plasticos",
        "domain": "plasticos",
        "response_dict": _converged().model_dump(),
        "settings": MagicMock(),
        "idempotency_key": IDEM_KEY,
        "emit_event": False,
        "graph_sync": False,
    }
    kwargs.update(overrides)
    return await coordinator.commit_after_enrich(**kwargs)


@pytest.fixture
def stubbed_effects():
    with (
        patch(
            "app.services.result_store.ResultStore.persist_enrich_response",
            new_callable=AsyncMock,
        ) as persist,
        patch("app.engines.packet_router.get_router") as router_factory,
    ):
        router = router_factory.return_value
        router.notify_graph_sync = AsyncMock(return_value=None)
        router.notify_score_invalidate = AsyncMock(return_value=None)
        yield {"persist": persist, "router": router}


async def test_required_persistence_failure_raises(stubbed_effects) -> None:
    """The coordinator signals the required failure explicitly, not in a log."""
    stubbed_effects["persist"].side_effect = RuntimeError("disk full")
    coordinator = SideEffectCoordinator()

    with pytest.raises(RequiredSideEffectError) as exc:
        await _commit(coordinator, require_persistence=True)

    assert "res.partner:55" in str(exc.value)
    assert coordinator._completed == set()


async def test_optional_persistence_failure_does_not_raise(stubbed_effects) -> None:
    """A caller that did not ask for the required contract keeps the old shape."""
    stubbed_effects["persist"].side_effect = RuntimeError("disk full")
    coordinator = SideEffectCoordinator()

    report = await _commit(coordinator, require_persistence=False)

    assert report.persisted is False
    assert any("persist:" in e for e in report.errors)
    # Still no completion marker — an unpersisted run is not a completed one,
    # whichever contract the caller asked for.
    assert coordinator._completed == set()


async def test_second_call_with_the_same_logical_key_is_skipped(stubbed_effects) -> None:
    """One logical operation commits its effects once."""
    coordinator = SideEffectCoordinator()

    first = await _commit(coordinator, require_persistence=True)
    second = await _commit(coordinator, require_persistence=True)

    assert first.skipped is False
    assert second.skipped is True
    assert stubbed_effects["persist"].await_count == 1


async def test_absent_logical_key_disables_dedupe_rather_than_faking_it(
    stubbed_effects,
) -> None:
    """ADR-EIE-008 — no entity-only collapse.

    Two genuine runs of the same entity, neither carrying a logical key, are two
    operations. The retired entity hash made the second one a no-op, which
    silently dropped a real enrichment.
    """
    coordinator = SideEffectCoordinator()

    first = await _commit(coordinator, idempotency_key=None, packet_id=None)
    second = await _commit(coordinator, idempotency_key=None, packet_id=None)

    assert first.key is None
    assert second.skipped is False
    assert stubbed_effects["persist"].await_count == 2


async def test_same_key_under_two_tenants_is_two_operations(stubbed_effects) -> None:
    """ADR-EIE-005 — tenant scoping reaches the in-process coordinator too."""
    coordinator = SideEffectCoordinator()

    await _commit(coordinator, tenant="tenant-a", require_persistence=True)
    second = await _commit(coordinator, tenant="tenant-b", require_persistence=True)

    assert second.skipped is False
    assert stubbed_effects["persist"].await_count == 2


async def test_concurrent_same_key_callers_are_serialised(stubbed_effects) -> None:
    """The per-key lock stops two in-process callers duplicating the work.

    This is an optimisation over the database constraint, not a replacement for
    it — a second process shares no lock — but within one process the second
    caller must observe the first's completion rather than racing past it.
    """
    import asyncio

    coordinator = SideEffectCoordinator()
    started = asyncio.Event()

    async def slow_persist(*args: Any, **kwargs: Any) -> None:
        started.set()
        await asyncio.sleep(0.05)

    stubbed_effects["persist"].side_effect = slow_persist

    results = await asyncio.gather(
        _commit(coordinator, require_persistence=True),
        _commit(coordinator, require_persistence=True),
    )

    assert stubbed_effects["persist"].await_count == 1
    assert sum(1 for r in results if r.skipped) == 1
