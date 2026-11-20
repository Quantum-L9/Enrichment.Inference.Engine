"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: enrichment
layer: [engines]
tags: [convergence, planning, evidence, acquisition]
owner: engine-team
status: active
--- /L9_META ---

Evidence-state tie-break for acquisition order (Campaign B, B3).

When the planner's own ordering leaves two gap fields equal, the one the run
knows less about is asked for first. This is an ordering among ties only: it
sets no budget, no variation count and no threshold, it names no field and no
domain, and it carries no weight to tune. The three ranks are positions, not
scores.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: No valid reply has spoken to the field.
NO_EVIDENCE = 2
#: Replies gave one value and the pass did not admit it.
WITHHELD_CLAIM = 1
#: Replies gave different values.
CONFLICTING = 0


def latest_withheld(
    passes: Iterable[Mapping[str, Mapping[str, Any]]],
) -> dict[str, Mapping[str, Any]]:
    """Each field's most recent withheld record across passes, in pass order."""
    latest: dict[str, Mapping[str, Any]] = {}
    for withheld in passes:
        latest.update(withheld)
    return latest


def evidence_tiebreak(
    gaps: Iterable[str], withheld: Mapping[str, Mapping[str, Any]]
) -> dict[str, int]:
    """Rank each gap field by how little the run knows about it; higher goes first.

    `withheld` is what valid replies said about fields a pass did not admit
    (`field_evidence.withheld_fields`). A gap it does not mention has no
    evidence. With nothing withheld every gap ranks the same, so the caller's
    order is unchanged.
    """
    ranks: dict[str, int] = {}
    for name in gaps:
        candidates = (withheld.get(name) or {}).get("candidates") or []
        if not candidates:
            ranks[name] = NO_EVIDENCE
        elif len(candidates) == 1:
            ranks[name] = WITHHELD_CLAIM
        else:
            ranks[name] = CONFLICTING
    return ranks
