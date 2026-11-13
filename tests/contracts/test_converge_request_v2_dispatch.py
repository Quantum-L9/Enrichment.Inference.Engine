"""Converge request v2 on the live handler (docs/contracts/converge/request.v2.md).

Real `handle_converge`, real convergence loop, the in-repo deterministic
provider and the checked-in `domains/plasticos` cartridge; only persistence, the
Gate router and the event emitter are replaced. In-process: no Gate round trip
and no writeback is claimed. The v1 path is pinned by test_converge_contract_v1.

Not marked `unit`: `app.engines.handlers` needs the provider runtime.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml

pytest.importorskip("perplexity", reason="app.engines.handlers needs the provider runtime")

from app.core.config import Settings
from app.engines import handlers
from app.engines.converge_request_v2 import REQUEST_CONTRACT_V2
from app.engines.domain_yaml_reader import DomainYamlReader
from app.models.schemas import EnrichResponse
from app.services.kb_resolver import KBResolver
from app.services.side_effect_coordinator import get_side_effect_coordinator

pytestmark = pytest.mark.asyncio

# Pinned to whatever revision is checked in, so a cartridge version bump on
# another branch of the stack does not turn this into a pin-mismatch refusal.
CARTRIDGE_VERSION: str = yaml.safe_load(
    (Path(__file__).resolve().parents[2] / "domains/plasticos/spec.yaml").read_bytes()
)["version"]

TARGETS = [
    {"field": "contamination_tolerance", "type": {"type": "float"}, "min_confidence": 0.8},
    {"field": "capacity_tons_month", "type": {"type": "float"}},
    {"field": "tier"},
]
TARGET_NAMES = {target["field"] for target in TARGETS}


def _v2(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "contract": REQUEST_CONTRACT_V2,
        "cartridge": {"id": "plasticos", "version": CARTRIDGE_VERSION},
        "node": "Facility",
        "targets": [dict(target) for target in TARGETS],
        "entity": {"id": "res.partner:55", "name": "Acme Recycling", "city": "Charlotte"},
        "object_type": "res.partner",
        "objective": "Enrich the facility profile",
        "max_variations": 3,
        "consensus_threshold": 0.8,
        "idempotency_key": "run:7:attempt:1",
    }
    body.update(overrides)
    return body


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    settings = Settings(l9_enrichment_provider="deterministic", l9_environment="test")
    monkeypatch.setattr(handlers, "_kb", KBResolver(settings.kb_dir))
    monkeypatch.setattr(handlers, "_idem", None)
    monkeypatch.setattr(
        handlers, "_domain_reader", DomainYamlReader(settings.domains_dir, require_published=False)
    )
    seen: dict[str, Any] = {}
    real_loop = handlers.run_convergence_loop

    async def spy_loop(**kwargs: Any) -> EnrichResponse:
        seen.update(kwargs)
        return await real_loop(**kwargs)

    with (
        patch("app.engines.handlers.get_settings", return_value=settings),
        patch("app.engines.handlers.run_convergence_loop", spy_loop),
        patch("app.services.result_store.ResultStore.persist_enrich_response") as persist,
        patch("app.engines.packet_router.get_router") as router_factory,
        patch("app.services.event_emitter.get_emitter") as emitter_factory,
    ):
        router_factory.return_value.notify_graph_sync = AsyncMock()
        router_factory.return_value.notify_score_invalidate = AsyncMock()
        emitter_factory.return_value.emit_enrichment_completed = AsyncMock()
        get_side_effect_coordinator().reset_for_tests()
        yield {"loop": seen, "persist": persist}
        get_side_effect_coordinator().reset_for_tests()


async def test_typed_targets_direct_the_run_from_the_pinned_cartridge(
    runtime: dict[str, Any],
) -> None:
    result = await handlers.handle_converge("plasticos", _v2())

    request = runtime["loop"]["request"]
    assert request.schema_ == {
        "contamination_tolerance": "float",
        "capacity_tons_month": "float",
        "tier": "string",
    }
    assert request.field_thresholds == {"contamination_tolerance": 0.8}
    assert runtime["loop"]["domain_spec"]["version"] == CARTRIDGE_VERSION
    assert "contamination_tolerance" in runtime["loop"]["domain_hints"]["priority_fields"]
    assert runtime["loop"]["inference_rules"]

    assert EnrichResponse.model_validate(result).model_dump() == result
    assert (result["state"], result["failure_reason"]) == ("completed", None)
    # Exactly the targets: no entity echo and no untargeted or derived field.
    # The values are the deterministic provider's synthetic ones, not real data.
    assert set(result["fields"]) == TARGET_NAMES
    assert isinstance(result["fields"]["contamination_tolerance"], float)
    assert isinstance(result["fields"]["capacity_tons_month"], float)
    assert isinstance(result["fields"]["tier"], str)
    assert runtime["persist"].call_count == 1


@pytest.mark.parametrize(
    ("tenant", "change", "code"),
    [
        ("tenant-a", {}, "tenant_cartridge_mismatch"),
        (
            "plasticos",
            {"cartridge": {"id": "plasticos", "version": "9.9.9"}},
            "cartridge_pin_mismatch",
        ),
        ("plasticos", {"targets": [{"field": "credit_score"}]}, "target_not_declared"),
        ("plasticos", {"contract": ...}, "v2_member_without_contract"),
    ],
)
async def test_refused_v2_request_runs_nothing(
    runtime: dict[str, Any], tenant: str, change: dict[str, Any], code: str
) -> None:
    body = {key: value for key, value in _v2(**change).items() if value is not ...}

    result = await handlers.handle_converge(tenant, body)

    assert result["state"] == "failed"
    assert result["failure_reason"].startswith(f"{code}: ")
    assert result["fields"] == {}
    assert runtime["loop"] == {}
    assert runtime["persist"].call_count == 0


# --- Response v2 wiring (CTO integration, response.v2 revision 1) ---


async def test_v2_request_is_answered_in_v2_with_resolution_and_declarations(
    runtime: dict[str, Any],
) -> None:
    result = await handlers.handle_converge(
        "plasticos", _v2(subject={"type": "facility", "role": "supplier"})
    )

    assert runtime["loop"]["response_contract"] == "eie.converge.response/v2"
    assert result["contract_version"] == "eie.converge.response/v2"
    assert EnrichResponse.model_validate(result).model_dump() == result
    resolution = result["resolution"]
    assert (resolution["cartridge_id"], resolution["node"]) == ("plasticos", "Facility")
    assert resolution["cartridge_version"] == CARTRIDGE_VERSION
    evidence = result["field_evidence"]
    assert set(evidence) >= TARGET_NAMES
    for name in TARGET_NAMES:
        assert evidence[name]["declaration"]["type"] in {"float", "string"}
        identity = evidence[name]["identity"]
        assert identity == {
            "subject_ref": "res.partner:55",
            "subject_type": "facility",
            "role": "supplier",
        }
    admitted = {n for n, e in evidence.items() if e["admitted"]}
    assert set(result["fields"]) == admitted


async def test_observation_is_evidence_only_and_disagreement_is_a_conflict(
    runtime: dict[str, Any],
) -> None:
    observation = {
        "field": "tier",
        "value": "observed-tier-from-history",
        "source_kind": "transaction",
        "source_ref": "sale.order:123",
    }
    result = await handlers.handle_converge("plasticos", _v2(observations=[observation]))

    entry = result["field_evidence"]["tier"]
    assert entry["admitted"] is False
    assert "tier" not in result["fields"]
    sources = [
        source
        for candidate in (entry.get("conflict") or {}).get("candidates", [entry])
        for source in candidate["sources"]
    ]
    assert any(source["source_type"] == "request_observation" for source in sources)
    assert entry["state"] in {"observed", "conflicting"}


async def test_v1_request_stays_v1(runtime: dict[str, Any]) -> None:
    body = _v2()
    v1 = {
        "entity": body["entity"],
        "object_type": "res.partner",
        "objective": body["objective"],
        "schema": {"name": "string", "city": "string"},
        "kb_context": "plasticos",
        "idempotency_key": "run:8:attempt:1",
    }
    result = await handlers.handle_converge("plasticos", v1)
    for key in ("contract_version", "field_evidence", "proposals", "resolution"):
        assert key not in result


async def test_v2_deadline_stop_is_answered_in_v2(
    runtime: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """response.v2 section 6 rule 4: a deadline stop is `degraded` in v2, not a v1 body."""
    import asyncio

    async def stalled_loop(**kwargs: Any) -> EnrichResponse:
        await asyncio.sleep(5)
        raise AssertionError("the deadline did not stop the loop")

    monkeypatch.setattr(handlers, "run_convergence_loop", stalled_loop)
    observation = {
        "field": "tier",
        "value": "gold",
        "source_kind": "transaction",
        "source_ref": "sale.order:123",
    }
    result = await handlers._handle_canonical_converge(
        "plasticos",
        _v2(subject={"type": "facility", "role": "supplier"}, observations=[observation]),
        budget_seconds=0.05,
    )

    assert result["state"] == "degraded"
    assert result["contract_version"] == "eie.converge.response/v2"
    assert EnrichResponse.model_validate(result).model_dump() == result
    assert (result["fields"], result["proposals"]) == ({}, [])
    assert result["resolution"]["cartridge_id"] == "plasticos"
    evidence = result["field_evidence"]
    assert set(evidence) >= TARGET_NAMES
    assert evidence["tier"]["state"] == "observed" and evidence["tier"]["admitted"] is False
    for name in TARGET_NAMES - {"tier"}:
        assert evidence[name]["state"] == "unknown"
        assert evidence[name]["unknown_reason"] == "budget_exhausted"
    assert runtime["persist"].call_count == 0


async def test_v1_deadline_stop_stays_v1(
    runtime: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    async def stalled_loop(**kwargs: Any) -> EnrichResponse:
        await asyncio.sleep(5)
        raise AssertionError("the deadline did not stop the loop")

    monkeypatch.setattr(handlers, "run_convergence_loop", stalled_loop)
    v1 = {
        "entity": {"id": "res.partner:55", "name": "Acme"},
        "object_type": "res.partner",
        "objective": "Enrich",
        "schema": {"name": "string"},
        "kb_context": "plasticos",
    }
    result = await handlers._handle_canonical_converge("plasticos", v1, budget_seconds=0.05)

    assert result["state"] == "degraded"
    for key in ("contract_version", "field_evidence", "proposals", "resolution"):
        assert key not in result
