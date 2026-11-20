"""EIE and CEG must declare the identical typed row contract for every sync endpoint.

Rule set ``eie-ceg-sync/v2``: the full typed row contract — target-node property
names and typed shapes (list ``items`` / map ``properties`` included), childsync
and taxonomyedges — is compared between EIE's spec and CEG's, and any difference
fails. The CEG side is a vendored snapshot of Cognitive.Engine.Graphs'
plasticos spec (tests/fixtures/ceg), pinned by source commit and sha256; it is
never built from EIE's own declarations (the v1 test that copied EIE's sync
block into a fake CEG spec proved nothing).

L9_META:
  tier: 2
  domain: native-domain
  authority: owner decision 2026-11-11 (typed spec contract)
  pr_class: app_code + tier2_test
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.engines.domain_spec_compatibility import (
    RULE_SET,
    check_compatibility,
    row_contract_summary,
    verify_pair_record,
)
from tests.contracts.ceg_revision import CEG_REVISION, CEG_SPECIMEN

ROOT = Path(__file__).resolve().parents[2]
EIE_SPEC = ROOT / "domains/plasticos/spec.yaml"
CEG_SNAPSHOT = ROOT / "tests/fixtures/ceg/plasticos.spec.yaml"
CEG_PROVENANCE = ROOT / "tests/fixtures/ceg/plasticos.spec.provenance.json"

pytestmark = pytest.mark.unit


def _eie() -> dict[str, Any]:
    return yaml.safe_load(EIE_SPEC.read_text())


def _ceg() -> dict[str, Any]:
    return yaml.safe_load(CEG_SNAPSHOT.read_text())


def _facility(spec: dict[str, Any]) -> list[dict[str, Any]]:
    node = next(n for n in spec["ontology"]["nodes"] if n["label"] == "Facility")
    return node["properties"]


def _prop(spec: dict[str, Any], name: str) -> dict[str, Any]:
    return next(p for p in _facility(spec) if p["name"] == name)


def test_rule_set_is_v2() -> None:
    assert RULE_SET == "eie-ceg-sync/v2"


def test_snapshot_is_pinned_to_its_source_commit_and_digest() -> None:
    provenance = json.loads(CEG_PROVENANCE.read_text())

    assert provenance["source_repository"] == "Quantum-L9/Cognitive.Engine.Graphs"
    assert provenance["source_commit"] == CEG_REVISION
    assert provenance["source_path"] == CEG_SPECIMEN
    assert len(provenance["source_commit"]) == 40
    assert hashlib.sha256(CEG_SNAPSHOT.read_bytes()).hexdigest() == provenance["snapshot_sha256"]
    header = CEG_SNAPSHOT.read_text().split("--- end header ---", 1)[0]
    assert provenance["source_commit"] in header
    assert provenance["source_sha256"] in header
    assert "REFRESH REQUIRED" in header


def test_checked_in_plasticos_spec_matches_cegs_typed_row_contract() -> None:
    result = check_compatibility(_eie(), _ceg())
    assert result["verdict"] == "pass", result["defects"]
    assert result["endpoints_checked"] == 1
    # Endpoints whose target nodes EIE does not enrich receive no EIE rows.
    assert "/v1/sync/facilities" not in result["ceg_endpoints_not_produced"]


def test_row_contract_carries_the_typed_childsync_field() -> None:
    summary = row_contract_summary(_eie(), "Facility", eie=True)
    assert summary["processed_polymers"] == "list<map{code,line_id,direction,form}>"
    assert summary["min_density"] == "float"
    # EIE-only evidence never rides a sync row.
    assert "city" not in summary and "material_grade" not in summary
    ceg = row_contract_summary(_ceg(), "Facility", eie=False)
    # Subset: every row property EIE sends is CEG's, with CEG's typed shape.
    assert summary.items() <= ceg.items()
    # CEG-owned properties EIE never produces are not declared, so never sent.
    assert "community_id" in ceg and "community_id" not in summary


def _rename(spec: dict[str, Any], old: str, new: str) -> None:
    _prop(spec, old)["name"] = new


DRIFTS = {
    # The pre-alignment EIE vocabulary, one drift at a time.
    "density_min": lambda eie, ceg: _rename(eie, "min_density", "density_min"),
    "mfi_max": lambda eie, ceg: _rename(eie, "max_mfi", "mfi_max"),
    "id": lambda eie, ceg: _rename(eie, "facility_id", "id"),
    "annual_capacity_mt": lambda eie, ceg: _rename(
        eie, "capacity_tons_month", "annual_capacity_mt"
    ),
    "facility_tier": lambda eie, ceg: _rename(eie, "tier", "facility_tier"),
    "polymer_types_untyped": lambda eie, ceg: _prop(eie, "processed_polymers").update(
        {"type": "list", "items": {"type": "string"}}
    ),
    "items_property_type": lambda eie, ceg: _prop(eie, "processed_polymers")["items"]["properties"][
        0
    ].update({"type": "int"}),
    "items_property_required": lambda eie, ceg: _prop(eie, "processed_polymers")["items"][
        "properties"
    ][0].pop("required"),
    "scalar_type": lambda eie, ceg: _prop(eie, "min_density").update({"type": "string"}),
    "alias_spelling": lambda eie, ceg: _prop(eie, "capacity_tons_month").update({"type": "number"}),
    "ceg_retyped": lambda eie, ceg: _prop(ceg, "tier").update({"type": "int"}),
    "required_flag": lambda eie, ceg: _prop(eie, "name").pop("required"),
    "undeclared_by_ceg": lambda eie, ceg: _facility(eie).append(
        {"name": "city_raw", "type": "string"}
    ),
    "evidence_promoted": lambda eie, ceg: _prop(eie, "city").pop("evidenceonly"),
    "ceg_owned_property_sent": lambda eie, ceg: _facility(eie).append(
        {"name": "community_id", "type": "string"}
    ),
    "childsync_missing": lambda eie, ceg: eie["sync"]["endpoints"][0].pop("childsync"),
    "childsync_edge": lambda eie, ceg: eie["sync"]["endpoints"][0]["childsync"][0].update(
        {"edgetype": "HANDLES"}
    ),
    "childsync_direction": lambda eie, ceg: eie["sync"]["endpoints"][0]["childsync"][0].update(
        {"edgedirection": "childtoparent"}
    ),
    "taxonomyedges": lambda eie, ceg: eie["sync"]["endpoints"][0].update(
        {
            "taxonomyedges": [
                {
                    "field": "tier",
                    "edgetype": "IN_TIER",
                    "targetlabel": "Tier",
                    "targetid": "code",
                }
            ]
        }
    ),
    "batchstrategy": lambda eie, ceg: eie["sync"]["endpoints"][0].pop("batchstrategy"),
    "tenant": lambda eie, ceg: ceg["domain"].update({"id": "other-tenant"}),
    "path": lambda eie, ceg: eie["sync"]["endpoints"][0].update({"path": "/v1/sync/absent"}),
    "method": lambda eie, ceg: eie["sync"]["endpoints"][0].update({"method": "PATCH"}),
    "targetnode": lambda eie, ceg: eie["sync"]["endpoints"][0].update({"targetnode": "Plant"}),
    "idproperty": lambda eie, ceg: eie["sync"]["endpoints"][0].update({"idproperty": "name"}),
    "eie_endpoint_without_row": lambda eie, ceg: eie["sync"]["endpoints"].append(
        {
            "path": "/v1/sync/polymerfamilies",
            "targetnode": "PolymerFamily",
            "idproperty": "code",
            "batchstrategy": "UNWINDMERGE",
        }
    ),
    "ceg_target_missing": lambda eie, ceg: ceg["ontology"]["nodes"].pop(0),
}


@pytest.mark.parametrize("drift", sorted(DRIFTS))
def test_every_row_contract_difference_fails(drift: str) -> None:
    eie, ceg = _eie(), _ceg()
    assert check_compatibility(eie, ceg)["verdict"] == "pass"
    DRIFTS[drift](eie, ceg)
    result = check_compatibility(eie, ceg)
    assert result["verdict"] == "fail"
    assert result["defects"]


def test_defects_name_the_field_and_both_shapes() -> None:
    eie = _eie()
    _rename(eie, "min_density", "density_min")
    _prop(eie, "processed_polymers")["items"]["properties"][0]["type"] = "int"
    defects = check_compatibility(eie, _ceg())["defects"]
    assert "/v1/sync/facilities: Facility.density_min: sent by EIE, not declared by CEG" in defects
    # Subset semantics: CEG's min_density, now unsent by EIE, is not a defect.
    assert not any("Facility.min_density" in d for d in defects)
    assert (
        "/v1/sync/facilities: Facility.processed_polymers[].code: type EIE 'int' != CEG 'string'"
        in defects
    )


def test_a_ceg_only_property_absent_from_eie_passes() -> None:
    """Subset: EIE need not declare CEG properties it never sends (CEG computes them)."""
    eie = _eie()
    _facility(eie).remove(_prop(eie, "tier"))
    result = check_compatibility(eie, _ceg())
    assert result["verdict"] == "pass", result["defects"]
    not_produced = result["ceg_properties_not_produced"]["/v1/sync/facilities"]
    assert {"tier", "community_id", "lat", "rate_per_ton"} <= set(not_produced)


def test_an_eie_property_ceg_does_not_declare_fails() -> None:
    eie = _eie()
    _facility(eie).append({"name": "plant_area_m2", "type": "float"})
    result = check_compatibility(eie, _ceg())
    assert result["verdict"] == "fail"
    assert result["defects"] == [
        "/v1/sync/facilities: Facility.plant_area_m2: sent by EIE, not declared by CEG"
    ]


def test_field_mappings_are_refused_not_honoured() -> None:
    result = check_compatibility(
        _eie(), _ceg(), field_mappings={"Facility": {"density_min": "min_density"}}
    )
    assert result["verdict"] == "fail"
    assert any("admits no field mappings" in d for d in result["defects"])


def test_ceg_only_policy_and_independent_versions_do_not_matter() -> None:
    eie, ceg = _eie(), _ceg()
    ceg["gates"] = [{"name": "ceg-only"}]
    ceg["domain"]["version"] = "9.9.9"
    result = check_compatibility(eie, ceg)
    assert result["verdict"] == "pass"
    assert result["eie_version"] != result["ceg_version"]


def test_pre_alignment_eie_spec_fails_against_the_ceg_snapshot() -> None:
    """The 0.3.0 EIE vocabulary (density_min, untyped lists, no childsync) is refused."""
    eie = copy.deepcopy(_eie())
    facility = _facility(eie)
    facility[:] = [
        {"name": "id", "type": "string"},
        {"name": "name", "type": "string"},
        {"name": "polymer_types", "type": "list", "items": {"type": "string"}},
        {"name": "density_min", "type": "float"},
        {"name": "annual_capacity_mt", "type": "float"},
        {"name": "facility_tier", "type": "string"},
    ]
    eie["sync"]["endpoints"][0].pop("childsync")
    result = check_compatibility(eie, _ceg())
    assert result["verdict"] == "fail"
    assert any("childsync" in d for d in result["defects"])
    assert any("Facility.density_min" in d for d in result["defects"])


def test_pair_record_binds_two_independent_hashes_and_refuses_single_engine_rollback() -> None:
    record = {
        "schema": "eie.domain-compatible-pair/v1",
        "tenant_id": "plasticos",
        "eie": {"sha256": "a" * 64, "spec_version": "0.4.0", "approval_ref": "fixture-eie"},
        "ceg": {"sha256": "b" * 64, "spec_version": "1.3.0", "approval_ref": "fixture-ceg"},
        "compatibility": {"rule_set": RULE_SET, "verdict": "pass", "proof_ref": "fixture"},
    }
    assert verify_pair_record(
        record, tenant_id="plasticos", eie_sha256="a" * 64, ceg_sha256="b" * 64
    )
    assert not verify_pair_record(
        record, tenant_id="plasticos", eie_sha256="c" * 64, ceg_sha256="b" * 64
    )
    assert not verify_pair_record(
        record, tenant_id="other", eie_sha256="a" * 64, ceg_sha256="b" * 64
    )
    # A pair proven under the retired v1 rule set no longer verifies.
    record["compatibility"]["rule_set"] = "eie-ceg-sync/v1"
    assert not verify_pair_record(
        record, tenant_id="plasticos", eie_sha256="a" * 64, ceg_sha256="b" * 64
    )
