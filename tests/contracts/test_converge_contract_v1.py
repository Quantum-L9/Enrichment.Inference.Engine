"""Converge contract v1 (docs/contracts/converge/v1.md), proven on the live handler.

The first block runs the real path end to end in process: real
`handle_converge`, real convergence loop, the in-repo deterministic provider and
the checked-in `domains/plasticos` spec. Only persistence, the Gate router and
the event emitter are replaced. The second block isolates the two response rules
with a stubbed loop. The consumer side is a vendored copy of the Odoo mapper
(fixtures/odoo_consumer/gate_mappers_vendored.py).

Not marked `unit`: `app.engines.handlers` needs the provider runtime, which the
constitution gate does not install (see test_canonical_converge_runtime_contract).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("perplexity", reason="app.engines.handlers needs the provider runtime")

from app.core.config import Settings
from app.engines import handlers
from app.engines.convergence_controller import DEADLINE_EXHAUSTED_REASON
from app.engines.domain_yaml_reader import DomainYamlReader
from app.models.schemas import EnrichResponse
from app.services.kb_resolver import KBResolver
from app.services.request_deadline import CANONICAL_CONVERGE_BUDGET_SECONDS
from app.services.side_effect_coordinator import get_side_effect_coordinator
from tests.contracts.fixtures.odoo_consumer.gate_mappers_vendored import (
    PARTNER_WRITEBACK_FIELD_ALLOWLIST,
    map_converge_response,
    partner_writeback_from_converge,
)

pytestmark = pytest.mark.asyncio

ENTITY_REF = "res.partner:55"
SCHEMA = dict.fromkeys(sorted(PARTNER_WRITEBACK_FIELD_ALLOWLIST), "string")


def _request(**overrides: Any) -> dict[str, Any]:
    """IB-Odoo_19 build_converge_request -> ConvergeRequest.to_dict, as emitted."""
    base: dict[str, Any] = {
        "entity": {
            "name": "Acme Recycling",
            "city": "Charlotte",
            "id": ENTITY_REF,
            "_odoo_entity_id": ENTITY_REF,
        },
        "object_type": "plasticos",
        "objective": "Full entity enrichment and inference",
        "max_variations": 5,
        "consensus_threshold": 0.80,
        "kb_context": "plasticos",
        "schema": dict(SCHEMA),
        "idempotency_key": "plasticos.enrichment.run:7:attempt:1",
        "odoo": {"model": "plasticos.enrichment.run", "record_id": 7, "db_name": "plasticos"},
    }
    base.update(overrides)
    return base


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """The real handler and loop on the deterministic provider; side effects recorded."""
    settings = Settings(l9_enrichment_provider="deterministic", l9_environment="test")
    reader = DomainYamlReader(settings.domains_dir, require_published=False)
    monkeypatch.setattr(handlers, "_kb", KBResolver(settings.kb_dir))
    monkeypatch.setattr(handlers, "_idem", None)
    monkeypatch.setattr(handlers, "_domain_reader", reader)
    with (
        patch("app.engines.handlers.get_settings", return_value=settings),
        patch("app.services.result_store.ResultStore.persist_enrich_response") as persist,
        patch("app.engines.packet_router.get_router") as router_factory,
        patch("app.services.event_emitter.get_emitter") as emitter_factory,
    ):
        router_factory.return_value.notify_graph_sync = AsyncMock(return_value=None)
        router_factory.return_value.notify_score_invalidate = AsyncMock(return_value=None)
        emitter_factory.return_value.emit_enrichment_completed = AsyncMock(return_value=None)
        get_side_effect_coordinator().reset_for_tests()
        yield {"persist": persist, "reader": reader}
        get_side_effect_coordinator().reset_for_tests()


@pytest.fixture
def stub_loop(runtime: dict[str, Any]) -> Iterator[AsyncMock]:
    with patch("app.engines.handlers.run_convergence_loop", new_callable=AsyncMock) as loop:
        yield loop


async def test_v1_request_completes_with_only_schema_fields_inside_the_budget(
    runtime: dict[str, Any],
) -> None:
    started = time.monotonic()
    result = await handlers.handle_converge("plasticos", _request())
    elapsed = time.monotonic() - started

    assert result["state"] == "completed"
    assert result["failure_reason"] is None
    assert result["fields"], "a completed v1 response carries the requested fields"
    assert set(result["fields"]) <= set(SCHEMA)
    assert elapsed < CANONICAL_CONVERGE_BUDGET_SECONDS == 25.0
    # The answer is the durable record: what was persisted is what was returned.
    assert runtime["persist"].call_count == 1
    assert EnrichResponse.model_validate(result).model_dump() == result


async def test_domain_spec_of_the_effective_revision_reaches_the_loop(
    runtime: dict[str, Any], stub_loop: AsyncMock
) -> None:
    stub_loop.return_value = EnrichResponse(state="completed", fields={"zip": "28202"})

    await handlers.handle_converge("plasticos", _request())

    spec = stub_loop.await_args.kwargs["domain_spec"]
    assert spec == runtime["reader"].load("plasticos").raw_spec
    assert spec, "the plasticos spec must not arrive empty"


async def test_replay_of_one_logical_operation_is_identical_and_persists_once(
    runtime: dict[str, Any],
) -> None:
    first = await handlers.handle_converge("plasticos", _request())
    second = await handlers.handle_converge("plasticos", _request())

    assert first["state"] == second["state"] == "completed"
    volatile = {"processing_time_ms"}
    assert {k: v for k, v in first.items() if k not in volatile} == {
        k: v for k, v in second.items() if k not in volatile
    }
    assert runtime["persist"].call_count == 1, "a replay must not persist a second record"


async def test_fields_outside_the_request_schema_are_not_returned_or_persisted(
    stub_loop: AsyncMock, runtime: dict[str, Any]
) -> None:
    stub_loop.return_value = EnrichResponse(
        state="completed",
        # The loop seeds from the entity and inference derives further fields.
        fields={"website": "https://acme.example", "id": ENTITY_REF, "annual_tonnage": 4200},
    )

    result = await handlers.handle_converge("plasticos", _request())

    assert result["fields"] == {"website": "https://acme.example"}
    persisted = runtime["persist"].call_args
    assert persisted is not None
    assert "annual_tonnage" not in repr(persisted), "the durable record must equal the answer"


async def test_an_empty_schema_names_no_field(stub_loop: AsyncMock) -> None:
    stub_loop.return_value = EnrichResponse(state="completed", fields={"zip": "28202"})

    result = await handlers.handle_converge("plasticos", _request(schema={}))

    assert result["fields"] == {}


async def test_deadline_exhaustion_is_degraded_never_a_completed_partial(
    stub_loop: AsyncMock, runtime: dict[str, Any]
) -> None:
    stub_loop.return_value = EnrichResponse(
        state="failed",
        failure_reason=DEADLINE_EXHAUSTED_REASON,
        fields={"website": "https://acme.example", "id": ENTITY_REF},
    )

    result = await handlers.handle_converge("plasticos", _request())

    assert result["state"] == "degraded"
    assert result["failure_reason"] == DEADLINE_EXHAUSTED_REASON
    assert set(result["fields"]) <= set(SCHEMA)
    assert runtime["persist"].call_count == 0, "a degraded result is not the durable record"


async def test_a_failure_that_is_not_the_deadline_stays_failed(stub_loop: AsyncMock) -> None:
    stub_loop.return_value = EnrichResponse(state="failed", failure_reason="no_valid_responses")

    result = await handlers.handle_converge("plasticos", _request())

    assert result["state"] == "failed"


async def test_odoo_consumer_parity_for_completed(runtime: dict[str, Any]) -> None:
    result = await handlers.handle_converge("plasticos", _request())

    mapped = map_converge_response(result)
    assert mapped["status"] == "ok"
    assert mapped["final_fields"] == result["fields"]
    # Every returned field is one Odoo may write: nothing is silently discarded.
    assert set(partner_writeback_from_converge(mapped)) == {
        name for name, value in result["fields"].items() if value not in (None, False, "")
    }
    assert mapped["total_cost_usd"] is None and mapped["writeback_applied"] is None


@pytest.mark.parametrize(
    ("state", "reason"),
    [("degraded", DEADLINE_EXHAUSTED_REASON), ("failed", "no_valid_responses")],
)
async def test_odoo_consumer_never_reads_a_non_completed_state_as_ok(
    state: str, reason: str
) -> None:
    response = EnrichResponse(state=state, failure_reason=reason, fields={"zip": "28202"})

    mapped = map_converge_response(response.model_dump())

    assert mapped["status"] == reason != "ok"
    assert mapped["state"] == state
