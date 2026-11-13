"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: enrichment
layer: [engines]
tags: [convergence, contract, converge-v1]
owner: engine-team
status: active
--- /L9_META ---

Converge contract v1 (docs/contracts/converge/v1.md): the two response rules
the convergence loop does not enforce itself. The loop seeds its working set
from the request entity and inference derives further fields, so `fields` must
be cut back to the request `schema`; and its deadline stop is `degraded`, not
`failed`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models.schemas import EnrichResponse
from .convergence_controller import DEADLINE_EXHAUSTED_REASON

CONVERGE_CONTRACT_VERSION = "v1"
COMPLETED_STATE = "completed"
DEGRADED_STATE = "degraded"
FAILED_STATE = "failed"


def restrict_fields_to_schema(
    result: dict[str, Any], schema: Mapping[str, str] | None
) -> list[str]:
    """Drop every `fields` entry the request `schema` did not name.

    Returns the dropped names, sorted. A request with no `schema` at all is
    outside contract v1 (the Odoo producer always sends one) and is left
    untouched; an empty `schema` names no field, so nothing is returned.
    """
    if schema is None:
        return []
    fields = result.get("fields") or {}
    dropped = sorted(name for name in fields if name not in schema)
    result["fields"] = {name: value for name, value in fields.items() if name in schema}
    return dropped


def degraded_response(reason: str) -> dict[str, Any]:
    """A deadline-stopped EnrichResponse that carries no result."""
    return EnrichResponse(state=DEGRADED_STATE, failure_reason=reason).model_dump()


def mark_deadline_degraded(result: dict[str, Any]) -> bool:
    """Relabel the loop's deadline stop from `failed` to `degraded`.

    The loop reports its own deadline stop as `failed` with
    `failure_reason == DEADLINE_EXHAUSTED_REASON`. Only that exact pair is
    relabelled; any other failure stays `failed`.
    """
    if (
        result.get("state") == FAILED_STATE
        and result.get("failure_reason") == DEADLINE_EXHAUSTED_REASON
    ):
        result["state"] = DEGRADED_STATE
        return True
    return False
