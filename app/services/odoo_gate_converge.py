"""Odoo Gate converge wire-format adapter.

Conforms to PlasticOS `plasticos_gate` contracts (IB-Odoo_19):
  - Request: ConvergeRequest.to_dict()
  - Response: map_converge_response / partner_writeback_from_converge

Do not invent partner fields. The allowlist is the hard writeback boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog

from app.models.schemas import EnrichRequest, EnrichResponse

logger = structlog.get_logger(__name__)

# Mirror plasticos_gate.services.gate_allowlists.PARTNER_WRITEBACK_FIELD_ALLOWLIST
PARTNER_WRITEBACK_FIELD_ALLOWLIST: frozenset[str] = frozenset(
    {
        "name",
        "website",
        "city",
        "zip",
        "street",
        "street2",
        "email",
        "phone",
    }
)

# Snapshot keys Odoo may send (all optional). source_urls seeds crawl targets.
PARTNER_SNAPSHOT_KEYS: frozenset[str] = PARTNER_WRITEBACK_FIELD_ALLOWLIST | frozenset(
    {"comment", "source_urls"}
)

DEFAULT_MAX_PASSES = 3
MAX_PASSES_CEILING = 10
DEFAULT_CONVERGE_TIMEOUT_SECONDS = 25.0

PARTNER_TARGET_SCHEMA: dict[str, str] = {
    "name": "string",
    "website": "string",
    "city": "string",
    "zip": "string",
    "street": "string",
    "street2": "string",
    "email": "string",
    "phone": "string",
}


def is_odoo_converge_payload(payload: dict[str, Any]) -> bool:
    """True when payload matches Odoo Gate ConvergeRequest shape."""
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("entity_snapshot"), dict):
        return True
    entity_id = payload.get("entity_id")
    return bool(isinstance(entity_id, str) and entity_id and "entity" not in payload)


def parse_odoo_converge_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Defensively parse Odoo ConvergeRequest.to_dict() into normalized fields."""
    if not isinstance(payload, dict):
        raise ValueError("converge payload must be a dict")

    entity_id = payload.get("entity_id")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise ValueError("converge: entity_id is required (e.g. 'res.partner:55')")
    entity_id = entity_id.strip()

    domain_raw = payload.get("domain")
    domain = (
        domain_raw.strip() if isinstance(domain_raw, str) and domain_raw.strip() else "plasticos"
    )

    snapshot_raw = payload.get("entity_snapshot")
    snapshot: dict[str, Any] = {}
    if isinstance(snapshot_raw, dict):
        for key, value in snapshot_raw.items():
            if key not in PARTNER_SNAPSHOT_KEYS:
                continue
            if value in (None, False, ""):
                continue
            if key == "source_urls":
                urls = _normalize_source_urls(value)
                if urls:
                    snapshot["source_urls"] = urls
                continue
            snapshot[key] = value

    odoo_ctx = payload.get("odoo")
    if not isinstance(odoo_ctx, dict):
        odoo_ctx = {}

    profile_id = payload.get("profile_id")
    if profile_id in (None, False, ""):
        profile_id = None
    elif not isinstance(profile_id, str):
        profile_id = str(profile_id)

    max_passes = _normalize_max_passes(payload.get("max_passes"))

    return {
        "entity_id": entity_id,
        "domain": domain,
        "entity_snapshot": snapshot,
        "odoo": odoo_ctx,
        "profile_id": profile_id,
        "max_passes": max_passes,
    }


def build_enrich_request(parsed: dict[str, Any]) -> EnrichRequest:
    """Map parsed Odoo converge request to internal EnrichRequest."""
    snapshot = dict(parsed["entity_snapshot"])
    source_urls = snapshot.pop("source_urls", None)
    entity: dict[str, Any] = {k: v for k, v in snapshot.items() if v not in (None, False, "")}
    if source_urls:
        entity["source_urls"] = source_urls

    object_type = _object_type_from_entity_id(parsed["entity_id"])
    objective = (
        "Backfill blank CRM partner fields from the entity snapshot and any "
        "source_urls. Return only confidently-resolved values for name, website, "
        "city, zip, street, street2, email, and phone."
    )
    if source_urls:
        objective += f" Seed research from these URLs: {', '.join(source_urls[:5])}."

    # EnrichRequest exposes the schema field via validation_alias="schema".
    return EnrichRequest.model_validate(
        {
            "entity": entity or {"name": parsed["entity_id"]},
            "object_type": object_type,
            "schema": dict(PARTNER_TARGET_SCHEMA),
            "objective": objective,
            "kb_context": parsed.get("domain") or "plasticos",
            "idempotency_key": _idempotency_key(parsed),
        }
    )


def filter_partner_fields(
    fields: dict[str, Any],
    *,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep allowlisted, non-empty partner fields.

    When snapshot is provided, skip keys that already have a non-empty value
    (Odoo merge-not-overwrite will ignore them; returning them adds noise).
    """
    existing = snapshot or {}
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in PARTNER_WRITEBACK_FIELD_ALLOWLIST:
            continue
        if value in (None, False, ""):
            continue
        if existing.get(key) not in (None, False, ""):
            continue
        out[key] = value
    return out


def build_odoo_converge_response(
    *,
    enrich_response: EnrichResponse,
    parsed: dict[str, Any],
    run_id: str | None = None,
) -> dict[str, Any]:
    """Build payload matching Odoo map_converge_response expectations."""
    snapshot = parsed.get("entity_snapshot") or {}
    raw_fields = enrich_response.fields or {}
    final_fields = filter_partner_fields(raw_fields, snapshot=snapshot)

    total_cost = _extract_total_cost_usd(enrich_response)
    status = "ok" if enrich_response.state == "completed" else "error"
    response: dict[str, Any] = {
        "run_id": run_id or new_run_id(),
        "status": status,
        "pass_count": int(enrich_response.pass_count or 0),
        "final_fields": final_fields,
        "writeback": {"partner_fields": dict(final_fields)},
        "total_tokens": int(enrich_response.tokens_used or 0),
        "total_cost_usd": total_cost,
    }
    if status != "ok":
        response["error"] = enrich_response.failure_reason or "convergence did not complete"
    return response


def build_odoo_converge_error(
    *,
    error: str,
    run_id: str | None = None,
    pass_count: int = 0,
    total_tokens: int = 0,
    total_cost_usd: float = 0.0,
) -> dict[str, Any]:
    """Structured non-ok payload for graceful Odoo local-pipeline fallback."""
    return {
        "run_id": run_id or new_run_id(),
        "status": "error",
        "pass_count": pass_count,
        "final_fields": {},
        "writeback": {"partner_fields": {}},
        "total_tokens": total_tokens,
        "total_cost_usd": total_cost_usd,
        "error": error,
    }


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"eie-{stamp}-{uuid4().hex[:8]}"


def _normalize_source_urls(value: Any) -> list[str]:
    if isinstance(value, str):
        url = value.strip()
        return [url] if url else []
    if not isinstance(value, list):
        return []
    urls: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            urls.append(item.strip())
    return urls


def _normalize_max_passes(value: Any) -> int:
    if value is None or value is False or value == "":
        return DEFAULT_MAX_PASSES
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        logger.warning("odoo_converge.invalid_max_passes", value=value)
        return DEFAULT_MAX_PASSES
    if parsed < 1:
        return 1
    return min(parsed, MAX_PASSES_CEILING)


def _object_type_from_entity_id(entity_id: str) -> str:
    if ":" in entity_id:
        return entity_id.split(":", 1)[0] or "res.partner"
    return "res.partner"


def _idempotency_key(parsed: dict[str, Any]) -> str | None:
    odoo = parsed.get("odoo") or {}
    correlation = odoo.get("correlation_id")
    if isinstance(correlation, str) and correlation.strip():
        return f"converge:{correlation.strip()}"
    entity_id = parsed.get("entity_id")
    if isinstance(entity_id, str) and entity_id:
        return f"converge:{entity_id}"
    return None


def _extract_total_cost_usd(response: EnrichResponse) -> float:
    feature_vector = response.feature_vector or {}
    cost_summary = feature_vector.get("cost_summary")
    if isinstance(cost_summary, dict):
        raw = cost_summary.get("total_cost_usd", 0.0)
        try:
            return float(raw or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0
