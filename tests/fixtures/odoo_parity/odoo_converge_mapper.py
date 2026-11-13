"""Vendored Odoo converge mapper (test fixture, not application code).

Transcribed from cryptoxdog/IB-Odoo_19 @ 3ad23289bd8fc8a8d10954bca9c2d4517d23a05c:
``plasticos_gate/services/gate_mappers.py`` lines 196-271 and
``plasticos_gate/services/gate_allowlists.py`` lines 17-26. The only change is
the return type: a plain dict whose keys are the fields of Odoo's
``ConvergeResponse`` dataclass. See README.md in this directory.
"""

from __future__ import annotations

from typing import Any

EIE_STATE_COMPLETED = "completed"
SDK_FAILURE_STATUS = "failed"

PARTNER_WRITEBACK_FIELD_ALLOWLIST = frozenset(
    {"name", "website", "city", "zip", "street", "street2", "email", "phone"}
)

#: ConvergeResponse fields copied from the payload key of the same name.
CARRIED_THROUGH = (
    "pass_count",
    "variation_count",
    "confidence",
    "consensus_threshold",
    "uncertainty_score",
    "processing_time_ms",
    "quality_tier",
    "inference_version",
    "kb_content_hash",
    "enrichment_payload",
    "feature_vector",
    "tokens_used",
)
#: ConvergeResponse fields copied as ``list(payload.get(key) or [])``.
CARRIED_AS_LIST = ("kb_files_consulted", "kb_fragment_ids", "inferences", "grade_matches")


def _sdk_failure_reason(payload: dict[str, Any]) -> str | None:
    if payload.get("status") != SDK_FAILURE_STATUS:
        return None
    error = payload.get("error")
    message = payload.get("message")
    error = error.strip() if isinstance(error, str) else ""
    message = message.strip() if isinstance(message, str) else ""
    if error and message and message != error:
        return f"{error}: {message}"
    return error or message or None


def map_converge_response(payload: dict[str, Any]) -> dict[str, Any]:
    raw_state = payload.get("state")
    state = raw_state if isinstance(raw_state, str) and raw_state.strip() else None
    failure_reason = payload.get("failure_reason") or _sdk_failure_reason(payload)
    if state == EIE_STATE_COMPLETED and not failure_reason:
        status = "ok"
    else:
        status = failure_reason or state or "failed"
    mapped: dict[str, Any] = {
        "status": status,
        "state": state,
        "failure_reason": failure_reason,
        "final_fields": payload.get("fields") or {},
    }
    mapped.update({key: payload.get(key) for key in CARRIED_THROUGH})
    mapped.update({key: list(payload.get(key) or []) for key in CARRIED_AS_LIST})
    mapped.update({"total_cost_usd": None, "writeback_applied": None, "raw": payload})
    return mapped


def partner_writeback_from_converge(mapped: dict[str, Any]) -> dict[str, Any]:
    source = mapped["final_fields"] or {}
    return {
        k: v
        for k, v in source.items()
        if k in PARTNER_WRITEBACK_FIELD_ALLOWLIST and v not in (None, False, "")
    }
