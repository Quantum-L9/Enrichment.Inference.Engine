"""A converge's graph field set carries observed and enriched values with provenance.

Operator ruling 2026-11-24, contract docs/contracts/data/graph_sync_intents.md
*Field set and provenance*. Through the live ``handle_converge`` and loop, with
the deterministic provider; the persistence/sync sink is captured.

- Full mode (gate_loop synthneutral, which declares ``band_provenance`` and the
  childsync item ``provenance``): the provider's ``band`` syncs ``enriched``; the
  request entity's ``handled_kinds`` syncs ``observed``, item-labelled.
- Legacy mode (the same cartridge without its provenance declarations): exactly
  ``admitted_fields`` as before -- no observed value, no label.
- PlasticOS stays legacy here and is the single-writer witness: the field set is
  byte-identical to ``shape_graph_fields(domain, admitted_fields(result))``, one
  ``processed_polymers`` list.

L9_META:
  tier: 2
  domain: graph-sync
  authority: CTO ASSIGN #351 rule revision (operator ruling 2026-11-24), PR-2
  pr_class: tier2_test
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SYNTH = REPO / "tests/e2e/gate_loop/cartridges/synthneutral"
KINDS = [{"code": "K1", "slot_id": "st-1-s1", "direction": "in"}]

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        importlib.util.find_spec("perplexity") is None,
        reason="app.engines.handlers needs the Perplexity SDK; not installed in the contract gate env",
    ),
]


def _synth(root: Path, *, provenance: bool) -> Path:
    target = root / "synthneutral"
    shutil.copytree(SYNTH, target)
    if not provenance:
        spec_path = target / "spec.yaml"
        spec = yaml.safe_load(spec_path.read_bytes())
        node = spec["ontology"]["nodes"][0]
        node["properties"] = [p for p in node["properties"] if p["name"] != "band_provenance"]
        for prop in node["properties"]:
            if prop["name"] == "handled_kinds":
                items = prop["items"]["properties"]
                prop["items"]["properties"] = [p for p in items if p["name"] != "provenance"]
        spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return root


async def _converge(root: Path, tenant: str, payload: dict[str, Any]) -> tuple[dict, dict]:
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
        patch.object(handlers, "_domain_reader", DomainYamlReader(root, require_published=False)),
        patch.object(handlers, "_kb", KBResolver(settings.kb_dir)),
        patch.object(handlers, "_idem", None),
        patch("app.engines.handlers.get_settings", return_value=settings),
        patch("app.engines.handlers._persist_and_sync", AsyncMock(side_effect=persist)),
    ):
        get_side_effect_coordinator().reset_for_tests()
        try:
            result = await handlers.handle_converge(tenant, payload)
        finally:
            get_side_effect_coordinator().reset_for_tests()
    return result, sink


def _station_payload() -> dict[str, Any]:
    return {
        "entity": {"id": "st-1", "station_id": "st-1", "handled_kinds": KINDS},
        "object_type": "station",
        "domain_id": "synthneutral",
        "node_label": "Station",
        "objective": "Enrich the station",
        "schema": {"band": "string"},
        "max_passes": 2,
    }


async def test_full_mode_syncs_enriched_and_observed_each_labelled(tmp_path: Path) -> None:
    from app.engines.domain_yaml_reader import DomainYamlReader
    from app.engines.packet_router import project_graph_sync_row, resolve_graph_sync_contract

    root = _synth(tmp_path, provenance=True)
    result, sink = await _converge(root, "synthneutral", _station_payload())

    assert result["state"] == "completed"
    assert "handled_kinds" not in result["fields"]  # response admission is unchanged
    fields = sink["graph_fields"]
    assert fields["band"] == result["fields"]["band"]
    assert fields["band_provenance"] == "enriched"
    assert fields["handled_kinds"] == [{**KINDS[0], "provenance": "observed"}]
    spec = DomainYamlReader(root, require_published=False).load("synthneutral").raw_spec
    contract = resolve_graph_sync_contract(spec, node_label="Station")
    projection = project_graph_sync_row(entity_id="st-1", fields=fields, contract=contract)
    assert projection.defects == () and projection.row is not None
    assert projection.row["handled_kinds"][0]["provenance"] == "observed"


async def test_legacy_mode_is_admitted_fields_with_no_observed_value(tmp_path: Path) -> None:
    from app.engines.handlers import admitted_fields

    root = _synth(tmp_path, provenance=False)
    result, sink = await _converge(root, "synthneutral", _station_payload())

    assert result["state"] == "completed"
    assert sink["graph_fields"] == admitted_fields(result)
    assert "handled_kinds" not in sink["graph_fields"]
    assert not any(name.endswith("_provenance") for name in sink["graph_fields"])


async def test_plasticos_single_writer_witness_is_byte_identical() -> None:
    from app.engines.handlers import admitted_fields
    from app.engines.polymer_capability import shape_graph_fields

    payload = {
        "entity": {
            "id": "facility:1",
            "processed_polymers": [
                {"code": "HDPE", "capability_id": "line-1", "direction": "in", "form": "Regrind"}
            ],
        },
        "object_type": "facility",
        "domain_id": "plasticos",
        "node_label": "Facility",
        "objective": "Enrich the facility",
        "schema": {"processed_polymers": "list", "capacity_tons_month": "float"},
        "max_passes": 2,
    }
    result, sink = await _converge(REPO / "domains", "plasticos", payload)

    assert result["state"] == "completed"
    expected = shape_graph_fields("plasticos", admitted_fields(result))
    assert sink["graph_fields"] == expected
    assert isinstance(sink["graph_fields"].get("processed_polymers"), list)
    assert not any(name.endswith("_provenance") for name in sink["graph_fields"])
