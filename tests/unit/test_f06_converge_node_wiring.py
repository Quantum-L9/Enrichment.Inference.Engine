"""F06 caller wiring, unit half: which node converge resolves, and that the loop passes it on.

The end-to-end proof (Carrier/Lane isolation, PlasticOS identity, no-rules
identity) is tests/contracts/test_converge_node_binding_contract.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from app.core.config import Settings
from app.engines import convergence_controller, handlers
from app.engines.domain_yaml_reader import DomainYamlReader
from app.models.schemas import EnrichRequest, EnrichResponse

REPO = Path(__file__).resolve().parents[2]
FREIGHT_ROOT = REPO / "tests/fixtures/cartridges"


async def _bundle_node(root: Path, domain_id: str | None, node_label: str | None) -> str | None:
    with patch.object(handlers, "_domain_reader", DomainYamlReader(root)):
        _hints, _rules, _spec, node = await handlers._load_domain_converge_bundle(
            domain_id=domain_id, node_label=node_label
        )
    return node


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("root", "domain_id", "node_label", "expected"),
    [
        (FREIGHT_ROOT, "freight-carriers", "Lane", "Lane"),
        (FREIGHT_ROOT, "freight-carriers", None, None),  # two nodes: no implicit one
        (REPO / "domains", "plasticos", None, "Facility"),  # the spec's only node
        (REPO / "domains", "plasticos", "Facility", "Facility"),
        (FREIGHT_ROOT, None, "Lane", None),  # no domain: nothing to bind to
        (FREIGHT_ROOT, "no-such-cartridge", "Lane", None),  # unloadable: no spec
    ],
)
async def test_the_bundle_resolves_the_node_the_request_addresses(
    root: Path, domain_id: str | None, node_label: str | None, expected: str | None
) -> None:
    assert await _bundle_node(root, domain_id, node_label) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("node_label", ["Carrier", None])
async def test_the_loop_hands_its_node_to_the_inference_bridge(node_label: str | None) -> None:
    built: list[dict[str, Any]] = []
    real = convergence_controller.InferenceBridge

    def spy(**kwargs: Any) -> Any:
        built.append(kwargs)
        return real(**kwargs)

    async def enricher(*_args: Any, **_kwargs: Any) -> EnrichResponse:
        return EnrichResponse(state="completed", fields={}, confidence=0.5, tokens_used=1)

    request = EnrichRequest(entity={"id": "e-1"}, object_type="carrier", objective="probe")
    with patch.object(convergence_controller, "InferenceBridge", spy):
        await convergence_controller.run_convergence_loop(
            request=request,
            settings=Settings(l9_environment="test"),
            kb_resolver=None,
            enricher=enricher,
            node_label=node_label,
        )
    assert [b["node"] for b in built] == [node_label]
