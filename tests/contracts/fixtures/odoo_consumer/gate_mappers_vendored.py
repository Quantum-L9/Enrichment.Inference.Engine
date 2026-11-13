"""Vendored copy of the Odoo consumer's converge-response expectations.

Source: cryptoxdog/IB-Odoo_19, branch Staging @ 3ad2328
  plasticos_gate/services/gate_mappers.py
    (file sha256 2cfccd65f3c485b04448522adbdfda10316fc80c81103189dc220b882e0697e6)
    ::_sdk_failure_reason, ::map_converge_response, ::partner_writeback_from_converge
  plasticos_gate/services/gate_allowlists.py::PARTNER_WRITEBACK_FIELD_ALLOWLIST

The status derivation and the writeback filter are copied verbatim. The result
is a plain dict instead of Odoo's ConvergeResponse dataclass, and the
provenance fields Odoo carries through unchanged are omitted. Nothing here
imports Odoo. Re-vendor when the source digest changes; this copy cannot detect
upstream drift by itself.
"""

from __future__ import annotations

from typing import Any

EIE_STATE_COMPLETED = "completed"
SDK_FAILURE_STATUS = "failed"
PARTNER_WRITEBACK_FIELD_ALLOWLIST = frozenset(
    {"name", "website", "city", "zip", "street", "street2", "email", "phone"}
)


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
    return {
        "status": status,
        "state": state,
        "failure_reason": failure_reason,
        "final_fields": payload.get("fields") or {},
        "total_cost_usd": None,
        "writeback_applied": None,
        "raw": payload,
    }


def partner_writeback_from_converge(mapped: dict[str, Any]) -> dict[str, Any]:
    source = mapped.get("final_fields") or {}
    return {
        k: v
        for k, v in source.items()
        if k in PARTNER_WRITEBACK_FIELD_ALLOWLIST and v not in (None, False, "")
    }
