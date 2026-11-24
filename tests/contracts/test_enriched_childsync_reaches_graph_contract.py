"""An enriched childsync collection reaches CEG as typed child rows (operator ruling 2026-11-24).

"Enrichments need to reach the graph" -- domain agnostic. A ``list<map>`` field
that a sync endpoint declares as ``childsync`` and that the converge ENRICHES
(provider evidence, admitted) is pinned for the graph leg as a list of maps and
projects onto the endpoint's typed row. A request-echoed value is still never
graph truth: the request entity here carries no collection at all.

Real: request resolution (v1 and v2), the cartridge reader, ``handle_converge``,
the convergence loop, validation, admission, the deterministic provider, and the
row projection. Replaced: the persistence/sync sink, captured instead of run.
Cartridge: the gate_loop synthneutral test cartridge; no domain name in core.

L9_META:
  tier: 2
  domain: graph-sync
  authority: CTO ASSIGN P18/P05 (operator ruling 2026-11-24, option C)
  pr_class: tier2_test
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

REPO = Path(__file__).resolve().parents[2]
CARTRIDGE = REPO / "tests/e2e/gate_loop/cartridges/synthneutral"
TENANT = "synthneutral"
NODE = "Station"
FIELD = "handled_kinds"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        importlib.util.find_spec("perplexity") is None,
        reason="app.engines.handlers needs the Perplexity SDK; not installed in the contract gate env",
    ),
]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    shutil.copytree(CARTRIDGE, tmp_path / TENANT)
    return tmp_path


def _entity() -> dict[str, Any]:
    # No `handled_kinds`: nothing the request carries can be echoed into the row.
    return {"id": "st-1", "station_id": "st-1", "title": "TEST_ONLY st-1"}


async def _converge(root: Path, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the live handler with the deterministic provider; return (response, sync kwargs)."""
    from app.core.config import Settings
    from app.engines import handlers
    from app.engines.domain_yaml_reader import DomainYamlReader
    from app.services.kb_resolver import KBResolver
    from app.services.side_effect_coordinator import SideEffectReport, get_side_effect_coordinator

    settings = Settings(l9_enrichment_provider="deterministic", l9_environment="test")
    sink: dict[str, Any] = {}

    async def persist(*_args: Any, **kwargs: Any) -> SideEffectReport:
        sink.update(kwargs)
        return SideEffectReport(key="captured")

    with (
        patch.object(handlers, "_domain_reader", DomainYamlReader(root)),
        patch.object(handlers, "_kb", KBResolver(settings.kb_dir)),
        patch.object(handlers, "_idem", None),
        patch("app.engines.handlers.get_settings", return_value=settings),
        patch("app.engines.handlers._persist_and_sync", AsyncMock(side_effect=persist)),
    ):
        get_side_effect_coordinator().reset_for_tests()
        try:
            result = await handlers.handle_converge(TENANT, payload)
        finally:
            get_side_effect_coordinator().reset_for_tests()
    return result, sink


def _declaration(root: Path) -> dict[str, Any]:
    from app.engines.domain_yaml_reader import DomainYamlReader

    spec = DomainYamlReader(root).load(TENANT).raw_spec
    props = next(n for n in spec["ontology"]["nodes"] if n["label"] == NODE)["properties"]
    prop = next(p for p in props if p["name"] == FIELD)
    return {k: prop[k] for k in ("type", "items") if k in prop}


def _assert_typed_child_row(root: Path, graph_fields: dict[str, Any]) -> None:
    from app.engines.domain_yaml_reader import DomainYamlReader
    from app.engines.packet_router import project_graph_sync_row, resolve_graph_sync_contract

    spec = DomainYamlReader(root).load(TENANT).raw_spec
    childsync = {c["field"]: c for e in spec["sync"]["endpoints"] for c in e.get("childsync", [])}
    assert FIELD in childsync
    value = graph_fields[FIELD]
    assert isinstance(value, list) and value
    assert all(isinstance(item, dict) for item in value)
    assert all(childsync[FIELD]["targetid"] in item for item in value)
    contract = resolve_graph_sync_contract(spec, node_label=NODE)
    assert contract is not None
    projection = project_graph_sync_row(entity_id="st-1", fields=graph_fields, contract=contract)
    assert projection.defects == ()
    assert projection.row is not None and projection.row[FIELD] == value


async def test_v2_an_enriched_childsync_collection_is_admitted_and_pinned_typed(root: Path) -> None:
    from app.engines.domain_yaml_reader import DomainYamlReader

    version = DomainYamlReader(root).load(TENANT).version
    payload = {
        "contract": "eie.converge.request/v2",
        "cartridge": {"id": TENANT, "version": version},
        "node": NODE,
        "targets": [{"field": FIELD}],
        "entity": _entity(),
        "object_type": "station",
        "objective": "Enrich the station",
        "idempotency_key": "childsync:v2",
    }
    result, sink = await _converge(root, payload)

    assert (result["state"], result["failure_reason"]) == ("completed", None)
    evidence = result["field_evidence"][FIELD]
    assert evidence["admitted"] is True and evidence["state"] != "observed"
    # Full mode (the cartridge declares item `provenance`): the synced items are
    # the response's items, each labelled `enriched` (operator ruling 2026-11-24).
    assert sink["graph_fields"][FIELD] == [
        {**item, "provenance": "enriched"} for item in result["fields"][FIELD]
    ]
    _assert_typed_child_row(root, sink["graph_fields"])


async def test_v1_a_typed_schema_target_is_enriched_and_pinned_typed(root: Path) -> None:
    payload = {
        "entity": _entity(),
        "object_type": "station",
        "domain_id": TENANT,
        "node_label": NODE,
        "objective": "Enrich the station",
        "schema": {FIELD: _declaration(root)},
        "max_passes": 2,
    }
    result, sink = await _converge(root, payload)

    assert (result["state"], result["failure_reason"]) == ("completed", None)
    # Full mode (the cartridge declares item `provenance`): the synced items are
    # the response's items, each labelled `enriched` (operator ruling 2026-11-24).
    assert sink["graph_fields"][FIELD] == [
        {**item, "provenance": "enriched"} for item in result["fields"][FIELD]
    ]
    _assert_typed_child_row(root, sink["graph_fields"])
