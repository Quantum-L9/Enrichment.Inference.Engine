"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: enrichment
layer: [engines]
tags: [convergence, contract, converge-response-v2, evidence, observations]
owner: engine-team
status: active
--- /L9_META ---

Evidence semantics of request v2.1 `observations` (Campaign B, B1).

An observation is something the consumer itself saw about one field, with the
record it saw it in. It becomes a response v2 evidence entry in state
`observed`, source type `request_observation`. Four things are never done:

* it is never admitted: an observation is evidence, not a result of this run;
* it is never scored: confidence is the consumer's own statement, or null;
* it is never generalized: one observation speaks for its own field only;
* disagreeing observations are never reconciled: they are `conflicting`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ...utils.safe_convert import safe_float

OBSERVATION_SOURCE_TYPE = "request_observation"


def _value_key(observation: Mapping[str, Any]) -> tuple[str, Any, str | None]:
    """Two observations agree only on the same value in the same stated unit."""
    value, unit = observation["value"], observation.get("unit")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ("number", safe_float(value), unit)
    return (type(value).__name__, repr(value), unit)


def _source(observation: Mapping[str, Any]) -> dict[str, Any]:
    source = {
        "source_type": OBSERVATION_SOURCE_TYPE,
        "source_ref": observation["source_ref"],
        "method": observation["source_kind"],
        # Whether two consumer records are independent is not known here.
        "independent": None,
    }
    if "observed_at" in observation:
        source["observed_at"] = observation["observed_at"]
    return source


def _valued(group: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Value, unit and consumer-stated confidence of observations that agree."""
    first = group[0]
    stated = {o["confidence"] for o in group if "confidence" in o}
    entry: dict[str, Any] = {
        "value": first["value"],
        "unit_status": "stated" if "unit" in first else "unknown",
        # One consumer-stated confidence is repeated; none, or several that
        # differ, is null. Nothing is averaged, picked or assigned.
        "confidence": next(iter(stated)) if len(stated) == 1 else None,
        "sources": [_source(o) for o in group],
    }
    if "unit" in first:
        entry["unit"] = first["unit"]
    return entry


def observation_evidence(
    observations: Iterable[Mapping[str, Any]], identity: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """One response v2 evidence entry per observed field, in first-seen order.

    `observations` are the validated records of a resolved v2 request
    (`ResolvedConvergeRequest.observations`). A field nobody observed gets no
    entry: absence here says nothing about that field.
    """
    by_field: dict[str, dict[Any, list[Mapping[str, Any]]]] = {}
    for observation in observations:
        groups = by_field.setdefault(observation["field"], {})
        groups.setdefault(_value_key(observation), []).append(observation)

    evidence: dict[str, dict[str, Any]] = {}
    for name, groups in by_field.items():
        candidates = [_valued(group) for group in groups.values()]
        if len(candidates) == 1:
            entry = {"state": "observed", "admitted": False, **candidates[0], "conflict": None}
        else:
            entry = {
                "state": "conflicting",
                "admitted": False,
                "confidence": None,
                "sources": [],
                "conflict": {"candidates": candidates, "resolution": None},
            }
        entry.update(derivation=None, unknown_reason=None, identity=dict(identity))
        evidence[name] = entry
    return evidence
