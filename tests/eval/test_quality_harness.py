"""The harness scores every labelled case as recorded, and fails closed."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from tests.eval import harness

_CASES_DIR = Path(__file__).resolve().parent / "cases"
# Declared, not discovered: a missing case file must fail, never shrink the run.
_CASE_IDS = ("supplier_material", "supplier_facility", "buyer_material", "buyer_facility")
_V1_FIXTURE = Path(__file__).resolve().parents[2] / "contracts" / "converge_response.json"


def _case(case_id: str) -> dict[str, Any]:
    loaded = json.loads((_CASES_DIR / f"{case_id}.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _keys(node: Any) -> set[str]:
    """Every mapping key anywhere in a report: a metric is named by its key."""
    if isinstance(node, dict):
        return set(node) | {key for value in node.values() for key in _keys(value)}
    if isinstance(node, list):
        return {key for item in node for key in _keys(item)}
    return set()


def _report() -> dict[str, Any]:
    cases = [_case(case_id) for case_id in _CASE_IDS]
    return harness.build_report([harness.score_case(case, case["output"]) for case in cases])


def test_case_set_is_exactly_the_declared_one_and_synthetic() -> None:
    assert sorted(p.stem for p in _CASES_DIR.glob("*.json")) == sorted(_CASE_IDS)
    cases = [_case(case_id) for case_id in _CASE_IDS]
    assert all(
        case["synthetic"] is True and case["case_id"] == cid
        for case, cid in zip(cases, _CASE_IDS, strict=True)
    )
    assert {(case["role"], case["subject_type"]) for case in cases} == {
        (role, kind)
        for role in ("supplier", "buyer")
        for kind in ("material_profile", "facility_profile")
    }


@pytest.mark.parametrize("case_id", _CASE_IDS)
def test_each_case_scores_every_labelled_field_as_recorded(case_id: str) -> None:
    case = _case(case_id)
    assert set(case["expected"]) == set(case["labels"]), (
        "every labelled field needs an expected outcome"
    )
    result = harness.score_case(case, case["output"])
    assert result["fields"] == case["expected"]
    assert sum(result["counts"].values()) == len(case["labels"])
    assert (result["role"], result["subject_type"]) == (case["role"], case["subject_type"])


def test_report_states_its_claim_level_and_computes_no_accuracy() -> None:
    report = _report()
    assert report["claim_level"] == "heuristic_quality"
    assert report["not_claimed"] == ["accuracy", "calibration", "live_outcome"]
    assert report["confidence_treatment"] == "uncalibrated_score_not_probability"
    assert set(report["totals"]) == set(harness.OUTCOMES)
    assert all(isinstance(count, int) for count in report["totals"].values())
    assert not _keys(report) & {"accuracy", "precision", "recall", "probability", "calibration"}
    assert all(count > 0 for count in report["totals"].values()), (
        "the cases must exercise every outcome"
    )


def test_report_separates_evidence_supported_from_heuristic_agreement() -> None:
    totals = _report()["totals"]
    assert totals["evidence_supported_correct"] == 7
    assert totals["heuristic_agreement"] == 2
    assert sum(totals.values()) == sum(len(_case(case_id)["labels"]) for case_id in _CASE_IDS)


def test_a_match_without_a_supporting_citation_is_only_agreement() -> None:
    case = _case("supplier_material")
    outputs = copy.deepcopy(case["output"])
    assert (
        harness.score_field(case, "material_form", outputs["material_form"])
        == "evidence_supported_correct"
    )
    outputs["material_form"]["source_refs"] = [
        "req:entity"
    ]  # real item, but it does not state this field
    assert (
        harness.score_field(case, "material_form", outputs["material_form"])
        == "heuristic_agreement"
    )
    outputs["material_form"]["source_refs"] = []
    assert (
        harness.score_field(case, "material_form", outputs["material_form"])
        == "heuristic_agreement"
    )
    # A heuristic label never becomes evidence-supported, whatever is cited.
    outputs["material_grade"]["source_refs"] = ["doc:s1", "req:entity"]
    assert (
        harness.score_field(case, "material_grade", outputs["material_grade"])
        == "heuristic_agreement"
    )


def test_confidence_is_listed_per_outcome_and_never_decides_an_outcome() -> None:
    case = _case("supplier_facility")
    result = harness.score_case(case, case["output"])
    assert result["confidence_scores_by_outcome"]["unknown_coerced"] == [0.95]
    outputs = copy.deepcopy(case["output"])
    for out in outputs.values():
        out["confidence"] = 0.01
    assert harness.score_case(case, outputs)["fields"] == result["fields"]


def test_harness_fails_closed_on_what_it_cannot_assess() -> None:
    case = _case("buyer_material")
    with pytest.raises(harness.CaseError, match="empty run"):
        harness.build_report([])
    with pytest.raises(harness.CaseError, match="no labels"):
        harness.score_case({**case, "labels": {}}, case["output"])
    with pytest.raises(harness.CaseError, match="unknown output state"):
        harness.score_field(case, "accepted_form", {"state": "verified", "value": "pellet"})
    bad_kind = {**case, "labels": {"accepted_form": {"kind": "guess"}}}
    with pytest.raises(harness.CaseError, match="unknown label kind"):
        harness.score_field(bad_kind, "accepted_form", case["output"]["accepted_form"])
    bad_basis = {
        **case,
        "labels": {"accepted_form": {"kind": "value", "value": "pellet", "basis": "vibes"}},
    }
    with pytest.raises(harness.CaseError, match="unknown label basis"):
        harness.score_field(bad_basis, "accepted_form", case["output"]["accepted_form"])
    assert harness.score_case(case, {})["counts"] == {"missing_output": len(case["labels"])}


def test_a_v1_response_reaches_heuristic_agreement_at_most() -> None:
    response = json.loads(_V1_FIXTURE.read_text(encoding="utf-8"))
    outputs = harness.outputs_from_v1_response(response)
    assert set(outputs) == set(response["fields"])
    assert all(out["source_refs"] == [] and out["confidence"] is None for out in outputs.values())
    case = {
        "case_id": "v1_view",
        "role": "supplier",
        "subject_type": "material_profile",
        "evidence": [{"ref": "doc:v1", "states": dict(response["fields"])}],
        "labels": {
            f: {"kind": "value", "value": v, "basis": "evidence"}
            for f, v in response["fields"].items()
        },
    }
    result = harness.score_case(case, outputs)
    assert set(result["fields"].values()) == {"heuristic_agreement"}
