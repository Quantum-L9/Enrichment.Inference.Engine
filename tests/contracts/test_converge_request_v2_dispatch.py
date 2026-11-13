"""Converge request v2 on the live handler (docs/contracts/converge/request.v2.md).

Real `handle_converge`, real convergence loop, the in-repo deterministic
provider and the checked-in `domains/plasticos` cartridge; only persistence, the
Gate router and the event emitter are replaced. In-process: no Gate round trip
and no writeback is claimed. The v1 path is pinned by test_converge_contract_v1.

Not marked `unit`: `app.engines.handlers` needs the provider runtime.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("perplexity", reason="app.engines.handlers needs the provider runtime")

from app.core.config import Settings
from app.engines import handlers
from app.engines.converge_request_v2 import REQUEST_CONTRACT_V2
from app.engines.domain_yaml_reader import DomainYamlReader
from app.models.schemas import EnrichResponse
from app.services.kb_resolver import KBResolver
from app.services.side_effect_coordinator import get_side_effect_coordinator

pytestmark = pytest.mark.asyncio

TARGETS = [
    {"field": "contamination_tolerance", "type": {"type": "float"}, "min_confidence": 0.8},
    {"field": "capacity_tons_month", "type": {"type": "float"}},
    {"field": "tier"},
]
TARGET_NAMES = {target["field"] for target in TARGETS}


def _v2(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "contract": REQUEST_CONTRACT_V2,
        "cartridge": {"id": "plasticos", "version": "0.4.0"},
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
    assert runtime["loop"]["domain_spec"]["version"] == "0.4.0"
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
