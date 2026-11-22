"""Contract: a value validation admits under its declaration is a valid sync row value.

``validation_engine.validate_response`` (provider reply -> typed fields) and
``packet_router.project_graph_sync_row`` (typed fields -> CEG row) read the same
declaration. This pins the seam between them: whatever validation admits under
a declared type projects with no row defect, and whatever validation refuses
never reaches the row. EIE-NATIVE-CLOSURE-001 lane L2 (E4; F04/F05/F22).

L9_META:
  tier: 2
  domain: graph-sync
  authority: EIE-NATIVE-CLOSURE-001 lane L2 (slice E4)
  pr_class: tier2_test
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.engines.domain_yaml_reader import DomainYamlReader
from app.engines.packet_router import PacketRouter, project_graph_sync_row
from app.services.validation_engine import validate_response
from tests.unit.test_eie_native_e4_typed_evidence import FULL_SCHEMA, NODE, SPEC, TENANT

pytestmark = [pytest.mark.unit, pytest.mark.enforcement]


@pytest.fixture
def contract(tmp_path: Path) -> Any:
    (tmp_path / TENANT).mkdir()
    (tmp_path / TENANT / "spec.yaml").write_text(yaml.safe_dump(SPEC), encoding="utf-8")
    router = PacketRouter(
        gate_url="https://gate-node:8080", domain_reader=DomainYamlReader(tmp_path)
    )
    resolved = router.graph_sync_contract(TENANT, node_label=NODE)
    assert resolved is not None
    return resolved


PROVIDER_VALUES: list[dict[str, Any]] = [
    {"members": [{"code": "A1", "line_id": 411, "extra": 1}, {"code": "B2", "direction": "in"}]},
    {"members": [{"code": "A1"}, {}]},
    {"members": [{"line_id": "L1"}]},
    {"members": ["A1", "B2"]},
    {"members": '[{"code": "A1"}]'},
    {"weight": True},
    {"weight": "0.94"},
    {"weight": math.nan},
    {"weight": "inf"},
    {"weight": 10**400},
    {"units": "12"},
    {"units": 3.7},
    {"units": 10**400},
]


@pytest.mark.parametrize("fields", PROVIDER_VALUES)
def test_what_validation_admits_projects_onto_the_row_without_defects(
    contract: Any, fields: dict[str, Any]
) -> None:
    validated = validate_response({"fields": fields, "confidence": 0.9}, FULL_SCHEMA)
    admitted = {k: v for k, v in validated.items() if k != "confidence"}
    projection = project_graph_sync_row(entity_id="widget:1", fields=admitted, contract=contract)
    assert projection.defects == (), projection.defects
    assert projection.row is not None
    # Refused fields never reach the row; admitted ones all do.
    assert set(projection.row) - {"widget_id"} == set(admitted)
