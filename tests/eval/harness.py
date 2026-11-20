"""Score field outputs against labelled synthetic cases.

The harness separates two things a match can mean:

* ``evidence_supported_correct``: the output equals an evidence-backed label
  AND cites a case evidence item that states that value.
* ``heuristic_agreement``: the output equals the label, but that support is not
  demonstrated (the label is a reviewer expectation, or the output cites no
  evidence item that states the value).

It reports counts per case. It computes no accuracy and treats confidence as an
uncalibrated score, never as a probability. See README.md for the claim level.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, TypeGuard

CLAIM_LEVEL = "heuristic_quality"
NOT_CLAIMED = ("accuracy", "calibration", "live_outcome")
CONFIDENCE_TREATMENT = "uncalibrated_score_not_probability"

VALUE_STATES = frozenset({"observed", "inferred", "external_claim"})
OUTPUT_STATES = VALUE_STATES | {"unknown", "conflicting"}
LABEL_KINDS = frozenset({"value", "unknown", "conflicting"})
LABEL_BASES = frozenset({"evidence", "heuristic"})

OUTCOMES = (
    "evidence_supported_correct",
    "heuristic_agreement",
    "disagreement",
    "unit_mismatch",
    "abstained",
    "unknown_preserved",
    "unknown_coerced",
    "unknown_as_conflict",
    "conflict_preserved",
    "conflict_coerced",
    "missing_output",
)


class CaseError(ValueError):
    """A case or output the harness cannot assess. Raised, never skipped."""


def same_value(left: Any, right: Any) -> bool:
    """JSON equality. A boolean never equals a number, though Python says ``True == 1``."""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return bool(left == right)


def usable_score(value: Any) -> TypeGuard[float]:
    """A JSON number in [0, 1]. ``0`` and ``1`` load as int; a boolean is not a score."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1


def _supporting_refs(case: dict[str, Any], field: str, value: Any) -> set[str]:
    return {
        e["ref"]
        for e in case["evidence"]
        if field in e["states"] and same_value(e["states"][field], value)
    }


def _score_value_label(
    case: dict[str, Any], field: str, label: dict[str, Any], out: dict[str, Any]
) -> str:
    if label["basis"] not in LABEL_BASES:
        raise CaseError(f"{case['case_id']}:{field}: unknown label basis {label['basis']!r}")
    if out["state"] not in VALUE_STATES:
        return "abstained"
    if not same_value(out["value"], label["value"]):
        return "disagreement"
    if out.get("unit") != label.get("unit"):
        return "unit_mismatch"
    cited = set(out.get("source_refs", ())) & _supporting_refs(case, field, label["value"])
    return (
        "evidence_supported_correct"
        if label["basis"] == "evidence" and cited
        else "heuristic_agreement"
    )


def score_field(case: dict[str, Any], field: str, out: dict[str, Any] | None) -> str:
    """Outcome of one labelled field. ``out`` is None when the output omits it."""
    label = case["labels"][field]
    if label["kind"] not in LABEL_KINDS:
        raise CaseError(f"{case['case_id']}:{field}: unknown label kind {label['kind']!r}")
    if out is None:
        return "missing_output"
    if out.get("state") not in OUTPUT_STATES:
        raise CaseError(f"{case['case_id']}:{field}: unknown output state {out.get('state')!r}")
    if label["kind"] == "value":
        return _score_value_label(case, field, label, out)
    if label["kind"] == "unknown":
        if out["state"] == "conflicting":
            return "unknown_as_conflict"
        return "unknown_preserved" if out["state"] == "unknown" else "unknown_coerced"
    if out["state"] == "conflicting":
        return "conflict_preserved"
    return "conflict_coerced" if out["state"] in VALUE_STATES else "abstained"


def score_case(case: dict[str, Any], outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Per-field outcomes and counts for one case. An empty label set is an error."""
    if not case["labels"]:
        raise CaseError(f"{case['case_id']}: no labels; nothing can be assessed")
    fields = {field: score_field(case, field, outputs.get(field)) for field in case["labels"]}
    scores: dict[str, list[float]] = {}
    for field, outcome in fields.items():
        confidence = outputs.get(field, {}).get("confidence")
        if usable_score(confidence):
            scores.setdefault(outcome, []).append(confidence)
    return {
        "case_id": case["case_id"],
        "role": case["role"],
        "subject_type": case["subject_type"],
        "fields": fields,
        "counts": dict(Counter(fields.values())),
        "unlabelled_outputs": sorted(set(outputs) - set(case["labels"])),
        "confidence_scores_by_outcome": scores,
    }


def build_report(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble the report. Counts only: no ratio is named or implied as accuracy."""
    if not results:
        raise CaseError("no case results; an empty run is not a passing run")
    totals: Counter[str] = Counter()
    for result in results:
        totals.update(result["counts"])
    return {
        "claim_level": CLAIM_LEVEL,
        "not_claimed": list(NOT_CLAIMED),
        "confidence_treatment": CONFIDENCE_TREATMENT,
        "cases": results,
        "totals": {outcome: totals.get(outcome, 0) for outcome in OUTCOMES},
    }


def outputs_from_v1_response(response: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """View a v1 converge response as harness outputs.

    v1 carries no per-field provenance, so every value is an uncited
    ``external_claim``: a v1 response can reach ``heuristic_agreement`` at most.
    Confidence is taken only where v1 states one per field; none is invented.
    """
    per_field = (response.get("feature_vector") or {}).get("per_field_confidence") or {}
    outputs: dict[str, dict[str, Any]] = {}
    for field, value in (response.get("fields") or {}).items():
        confidence = per_field.get(field)
        outputs[field] = {
            "state": "external_claim",
            "value": value,
            "confidence": confidence if usable_score(confidence) else None,
            "source_refs": [],
        }
    return outputs
