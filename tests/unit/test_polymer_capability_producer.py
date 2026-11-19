"""Odoo capability lines are the fields CEG's PROCESSES gate consumes.

The Odoo models (read at 3d7212355cfa7db838807c2f39181cf95473ca76) store the
polymer on ``plasticos.polymer.code`` / ``plasticos.material.profile.polymer``,
buyer entry as ``direction == "in"``, and the line as ``capability_id``
(``plasticos.capability``). CEG's ``polymer_capability_gate`` reads the polymer
code, ``direction`` equal to ``in``, and the PROCESSES relationship identified
by ``line_id``. No mapping matrix and no match policy live here.

L9_META:
  tier: 2
  domain: graph-sync
  authority: ceg processes gate field transport
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.engines.packet_router import (
    GRAPH_SYNC_SOURCE_DOMAIN_SPEC,
    GraphSyncContract,
    project_graph_sync_row,
)
from app.engines.polymer_capability import prepare_processed_polymers
from app.engines.spec_types import row_properties

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CEG_SNAPSHOT = ROOT / "tests/fixtures/ceg/plasticos.spec.yaml"


def _ceg() -> dict[str, Any]:
    text = CEG_SNAPSHOT.read_text(encoding="utf-8")
    body = text.split("--- end header ---", 1)[1]
    loaded = yaml.safe_load(body)
    assert isinstance(loaded, dict)
    return loaded


def _contract() -> GraphSyncContract:
    spec = yaml.safe_load((ROOT / "domains/plasticos/spec.yaml").read_text(encoding="utf-8"))
    schema = row_properties(spec, "Facility")
    assert schema is not None
    return GraphSyncContract(
        entity_type="facilities",
        id_property="facility_id",
        source=GRAPH_SYNC_SOURCE_DOMAIN_SPEC,
        row_schema=schema,
    )


def test_processes_gate_consumes_code_direction_in_and_the_capability_line() -> None:
    spec = _ceg()
    gate = next(item for item in spec["gates"] if item["name"] == "polymer_capability_gate")
    assert gate["type"] == "traversal"
    assert gate["edgetype"] == "PROCESSES"
    assert gate["candidateprop"] == "code"
    assert {"property": "direction", "equals": "in"} in gate["edgepredicates"]
    child = next(
        item for item in spec["sync"]["endpoints"] if item["path"] == "/v1/sync/facilities"
    )
    sync = child["childsync"][0]
    assert sync["edgetype"] == "PROCESSES"
    assert sync["edgeidentity"] == "line_id"
    assert "line_id" in sync["edgeproperties"]
    assert "direction" in sync["edgeproperties"]


def test_odoo_capability_line_reaches_the_sync_row_and_policy_fields_do_not() -> None:
    profiles = [
        {
            "polymer": "HDPE",
            "direction": "in",
            "capability_id": [17, "Shredder"],
            "material_grade_id": [3, "Totes"],
            "color_ids": [1, 2],
            "rate_lb_hr": 1000.0,
            "form": "regrind",
        },
        {
            "polymer_id": {"id": 4, "code": "PP"},
            "direction": "out",
            "capability_id": 18,
            "rated_capacity_lb_hr": 2000,
        },
        {
            "polymer": "PET",
            "direction": "in",
            "capability_id": False,
        },
    ]
    prepared = prepare_processed_polymers(profiles)
    assert prepared == [
        {"code": "HDPE", "line_id": "17", "direction": "in", "form": "regrind"},
        {"code": "PP", "line_id": "18", "direction": "out"},
    ]
    raw = project_graph_sync_row(
        entity_id="fac-1",
        fields={"processed_polymers": profiles},
        contract=_contract(),
    )
    assert raw.row is None
    assert raw.defects
    projection = project_graph_sync_row(
        entity_id="fac-1",
        fields={"processed_polymers": prepared},
        contract=_contract(),
    )
    assert projection.defects == ()
    assert projection.row is not None
    assert projection.row["processed_polymers"] == prepared
    sent = projection.row["processed_polymers"]
    assert all("material_grade_id" not in item for item in sent)
    assert all("color_ids" not in item and "rate_lb_hr" not in item for item in sent)
    assert "form" not in sent[1]


def test_a_wire_element_keeps_line_id_and_direction() -> None:
    """The declaration used to keep only code, so the line never reached CEG."""
    elements = [
        {"code": "HDPE", "line_id": "17", "direction": "in", "note": "dropped"},
        {"code": "HDPE", "line_id": "18", "direction": "in"},
    ]
    assert prepare_processed_polymers(elements) == elements
    projection = project_graph_sync_row(
        entity_id="fac-1",
        fields={"processed_polymers": elements},
        contract=_contract(),
    )
    assert projection.row is not None
    assert projection.row["processed_polymers"] == [
        {"code": "HDPE", "line_id": "17", "direction": "in"},
        {"code": "HDPE", "line_id": "18", "direction": "in"},
    ]
