"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: enrichment
layer: [engines]
tags: [convergence, contract, converge-response-v2, evidence]
owner: engine-team
status: active
--- /L9_META ---

Converge response v2 (docs/contracts/converge/response.v2.md, frozen 2026-11-13):
one evidence entry per field, saying what kind of evidence stands behind it.

Everything here reads what the loop already recorded. It decides nothing: a
field is `admitted` exactly when the loop put it in `fields`, a confidence is
the score the tracker holds, and a field with nothing behind it is an explicit
`unknown`. Unknown and conflicting fields carry no value. Nothing is defaulted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ...utils.safe_convert import normalize_confidence, safe_float

RESPONSE_CONTRACT_V2 = "eie.converge.response/v2"

# Sources the loop records for a field's latest value (convergence_controller).
CONSENSUS_SOURCE = "consensus"
INFERENCE_SOURCE = "inference"
GRAPH_SOURCE = "graph_inference"

_VALUE_STATES = frozenset({"observed", "inferred", "external_claim"})
_REVIEW_REASONS = (
    ("contains_external_claim", lambda e: e["state"] == "external_claim"),
    ("contains_inferred", lambda e: e["state"] == "inferred"),
    ("contains_conflict", lambda e: e["state"] == "conflicting"),
    ("contains_unknown", lambda e: e["state"] == "unknown"),
    (
        "contains_unadmitted",
        lambda e: e["state"] in ("inferred", "external_claim") and not e["admitted"],
    ),
)


class EvidenceContractError(ValueError):
    """The loop's record cannot be stated as a valid v2 body. Never repaired."""


def _present(value: Any) -> bool:
    return value is not None and value not in ("", [], {})


def _value_key(value: Any) -> tuple[str, Any]:
    """Same identity the consensus engine uses: 3 and 3.0 agree, text is exact."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ("number", safe_float(value))
    return ("text", str(value))


def withheld_fields(
    replies: Iterable[Mapping[str, Any]], admitted: Iterable[str], requested: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """What valid provider replies said about fields the pass did not admit.

    One distinct value is a claim that did not clear its bar; several are a
    conflict. Each candidate carries the strongest confidence a reply stated
    for exactly that value. A requested field no reply mentioned is absent.
    """
    admitted_names, wanted = set(admitted), set(requested)
    seen: dict[str, dict[tuple[str, Any], tuple[Any, float]]] = {}
    for reply in replies:
        confidence = normalize_confidence(reply.get("confidence"))
        for name, value in reply.items():
            if name == "confidence" or name in admitted_names or not _present(value):
                continue
            if wanted and name not in wanted:
                continue
            best = seen.setdefault(name, {}).get(_value_key(value))
            if best is None or confidence > best[1]:
                seen[name][_value_key(value)] = (value, confidence)
    return {
        name: {"candidates": [{"value": v, "confidence": c} for v, c in values.values()]}
        for name, values in seen.items()
    }


def _source(source_type: str, ref: str, method: str) -> dict[str, Any]:
    # Independence between sources is unknown today, and stated as such.
    return {"source_type": source_type, "source_ref": ref, "method": method, "independent": None}


def _valued(state: str, value: Any, confidence: float | None, admitted: bool) -> dict[str, Any]:
    # No unit is recorded for any value yet: unknown, never guessed.
    return {
        "state": state,
        "admitted": admitted,
        "value": value,
        "unit_status": "unknown",
        "confidence": confidence,
    }


def _produced(
    name: str, record: Mapping[str, Any], provenance: Mapping[str, Any] | None, admitted: bool
) -> dict[str, Any]:
    source, pass_ref = record["source"], f"pass:{record['pass_number']}"
    confidence = normalize_confidence(record["confidence"])
    if source == INFERENCE_SOURCE:
        if not provenance or not provenance.get("rule_id") or not provenance.get("rule_version"):
            raise EvidenceContractError(f"{name}: inferred without a rule id and version")
        entry = _valued("inferred", record["value"], confidence, admitted)
        entry["sources"] = []
        entry["derivation"] = {
            "method": str(provenance["rule_id"]),
            "rule_or_model_version": str(provenance["rule_version"]),
            "inputs": sorted(provenance.get("inputs") or {}),
            "kb_fragment_ids": [],
        }
        return entry
    if source == CONSENSUS_SOURCE:
        origin = _source("provider", f"{pass_ref}/consensus", "provider_consensus")
    elif source == GRAPH_SOURCE:
        origin = _source("graph", f"{pass_ref}/graph_inference", "graph_inference_target")
    else:
        raise EvidenceContractError(f"{name}: unrecognized value source {source!r}")
    entry = _valued("external_claim", record["value"], confidence, admitted)
    entry["sources"] = [origin]
    return entry


def _withheld(name: str, withheld: Mapping[str, Any]) -> dict[str, Any]:
    candidates = list(withheld.get("candidates") or [])
    ref = f"pass:{withheld.get('pass_number', 0)}/replies"
    sources = [_source("provider", ref, "provider_reply")]
    if not candidates:
        raise EvidenceContractError(f"{name}: withheld with no candidate")
    if len(candidates) == 1:
        only = candidates[0]
        entry = _valued(
            "external_claim", only["value"], normalize_confidence(only["confidence"]), False
        )
        entry["sources"] = sources
        return entry
    return {
        "state": "conflicting",
        "admitted": False,
        "confidence": None,
        "sources": [],
        "conflict": {
            "candidates": [
                {
                    "value": c["value"],
                    "unit_status": "unknown",
                    "confidence": normalize_confidence(c["confidence"]),
                    "sources": sources,
                }
                for c in candidates
            ],
            "resolution": None,
        },
    }


def build_field_evidence(
    *,
    requested: Iterable[str],
    fields: Mapping[str, Any],
    produced: Mapping[str, Mapping[str, Any]],
    provenance: Mapping[str, Mapping[str, Any]],
    withheld: Mapping[str, Mapping[str, Any]],
    entity: Mapping[str, Any],
    identity: Mapping[str, Any],
    unknown_reason: str,
) -> dict[str, dict[str, Any]]:
    """One entry per requested field and per field in `fields`.

    `produced` is the tracker's latest record per field (value, confidence,
    source, pass_number). Precedence: a value the loop holds, then what the
    replies said and the pass withheld, then the request's own value, then
    `unknown`.
    """
    evidence: dict[str, dict[str, Any]] = {}
    for name in dict.fromkeys([*requested, *fields]):
        if name in produced:
            entry = _produced(name, produced[name], provenance.get(name), name in fields)
        elif name in fields:
            raise EvidenceContractError(f"{name}: in fields with no recorded source")
        elif name in withheld:
            entry = _withheld(name, withheld[name])
        elif _present(entity.get(name)):
            # The consumer's own value: evidence only, never a result, never scored.
            entry = _valued("observed", entity[name], None, False)
            entry["sources"] = [_source("request_entity", f"entity.{name}", "consumer_supplied")]
        else:
            entry = {"state": "unknown", "admitted": False, "confidence": None, "sources": []}
        entry.setdefault("derivation", None)
        entry.setdefault("conflict", None)
        entry["unknown_reason"] = unknown_reason if entry["state"] == "unknown" else None
        entry["identity"] = dict(identity)
        evidence[name] = entry
    return evidence


def build_proposals(
    evidence: Mapping[str, Mapping[str, Any]], identity: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """One reviewable proposal for the request's subject; none without a subject type."""
    if not evidence or not identity.get("subject_type"):
        return []
    entries = list(evidence.values())
    return [
        {
            "proposal_id": "p1",
            "proposal_type": identity["subject_type"],
            "identity": dict(identity),
            "fields": list(evidence),
            "admitted_fields": [name for name, e in evidence.items() if e["admitted"]],
            "review": {
                "required": True,
                "reasons": [reason for reason, test in _REVIEW_REASONS if any(map(test, entries))],
            },
        }
    ]


def check_response_v2(body: Mapping[str, Any], requested: Iterable[str]) -> None:
    """Raise EvidenceContractError unless `body` obeys response.v2 section 6 (producer)."""
    evidence, fields = body["field_evidence"], body["fields"]
    missing = sorted(set(requested) - set(evidence))
    if missing:
        raise EvidenceContractError(f"requested fields without an entry: {missing}")
    admitted = {name: e["value"] for name, e in evidence.items() if e["admitted"]}
    if admitted != dict(fields):
        raise EvidenceContractError("fields is not exactly the admitted entries")
    for name, entry in evidence.items():
        valued = entry["state"] in _VALUE_STATES
        if valued != ("value" in entry) or (not valued and entry["admitted"]):
            raise EvidenceContractError(f"{name}: value presence does not match {entry['state']}")
        if entry["state"] == "observed" and entry["admitted"]:
            raise EvidenceContractError(f"{name}: an observed value is never admitted")
    if body["state"] == "failed" and fields:
        raise EvidenceContractError("a failed response admits nothing")
    if body["state"] != "completed" and body["proposals"]:
        raise EvidenceContractError("only a completed response proposes")
