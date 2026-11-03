"""Independent native schemas must agree only at their transport intersection.

L9_META:
  tier: 2
  domain: native-domain
  authority: native-v3 cross-engine contract and user decision 2026-11-03
  pr_class: app_code + tier2_test
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from app.engines.domain_spec_compatibility import check_compatibility, verify_pair_record


def _specs():
    eie = {
        "name": "plasticos",
        "version": "0.3.0",
        "ontology": {
            "nodes": [
                {
                    "label": "Facility",
                    "properties": [
                        {"name": "name", "type": "string"},
                        {"name": "id", "type": "string"},
                    ],
                }
            ]
        },
        "sync": {
            "endpoints": [
                {
                    "path": "/v1/sync/facilities",
                    "targetnode": "Facility",
                    "idproperty": "facility_id",
                }
            ]
        },
    }
    ceg = {
        "domain": {"id": "plasticos", "version": "1.3.0-reviewed"},
        "ontology": {
            "nodes": [
                {
                    "label": "Facility",
                    "properties": [
                        {"name": "name", "type": "string"},
                        {"name": "facility_id", "type": "string"},
                    ],
                }
            ]
        },
        "sync": copy.deepcopy(eie["sync"]),
        "gates": [{"name": "ceg-only"}],
        "scoring": {"dimensions": [{"name": "ceg-only"}]},
    }
    return eie, ceg


@pytest.mark.unit
def test_independent_versions_and_ceg_ranking_do_not_require_identical_yaml():
    eie, ceg = _specs()
    result = check_compatibility(eie, ceg)
    assert result["verdict"] == "pass"
    assert result["eie_version"] != result["ceg_version"]
    assert result["endpoints_checked"] == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "mutation", ["tenant", "path", "method", "targetnode", "idproperty", "type", "missing_id"]
)
def test_each_transport_conflict_is_refused(mutation):
    eie, ceg = _specs()
    endpoint = ceg["sync"]["endpoints"][0]
    if mutation == "tenant":
        ceg["domain"]["id"] = "other-tenant"
    elif mutation in {"path", "method", "targetnode", "idproperty"}:
        endpoint[mutation] = {
            "path": "/v1/sync/absent",
            "method": "PUT",
            "targetnode": "Other",
            "idproperty": "other_id",
        }[mutation]
    elif mutation == "type":
        ceg["ontology"]["nodes"][0]["properties"][0]["type"] = "float"
    elif mutation == "missing_id":
        ceg["ontology"]["nodes"][0]["properties"].pop()
    assert check_compatibility(eie, ceg)["verdict"] == "fail"


@pytest.mark.unit
def test_declared_mapping_is_checked_but_synonyms_are_never_guessed():
    eie, ceg = _specs()
    eie["ontology"]["nodes"][0]["properties"].append({"name": "density_min", "type": "float"})
    ceg["ontology"]["nodes"][0]["properties"].append({"name": "min_density", "type": "float"})
    mapping = {"Facility": {"density_min": "min_density"}}
    assert check_compatibility(eie, ceg, field_mappings=mapping)["verdict"] == "pass"
    mapping["Facility"]["density_min"] = "unknown_target"
    assert check_compatibility(eie, ceg, field_mappings=mapping)["verdict"] == "fail"


@pytest.mark.unit
def test_pair_record_binds_two_independent_hashes_and_refuses_single_engine_rollback():
    record = {
        "schema": "eie.domain-compatible-pair/v1",
        "tenant_id": "plasticos",
        "eie": {"sha256": "a" * 64, "spec_version": "0.3.0", "approval_ref": "fixture-eie"},
        "ceg": {"sha256": "b" * 64, "spec_version": "1.3.0", "approval_ref": "fixture-ceg"},
        "compatibility": {"rule_set": "eie-ceg-sync/v1", "verdict": "pass", "proof_ref": "fixture"},
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


@pytest.mark.unit
def test_checked_in_plasticos_native_endpoint_intersection():
    root = Path(__file__).resolve().parents[2]
    eie = yaml.safe_load((root / "domains/plasticos/spec.yaml").read_text())
    # A compact independent graph contract fixture. Full CEG native validation
    # is exercised separately by the explicit operator CLI, not a dependency.
    _, ceg = _specs()
    ceg["sync"] = copy.deepcopy(eie["sync"])
    for endpoint in ceg["sync"]["endpoints"][1:]:
        ceg["ontology"]["nodes"].append(
            {
                "label": endpoint["targetnode"],
                "properties": [{"name": endpoint["idproperty"], "type": "string"}],
            }
        )
    assert check_compatibility(eie, ceg)["verdict"] == "pass"
