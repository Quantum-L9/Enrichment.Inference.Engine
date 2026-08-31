"""Odoo Gate converge wire-format adapter.

Conforms to PlasticOS `plasticos_gate` contracts (IB-Odoo_19):
  - Request: ConvergeRequest.to_dict()
  - Response: map_converge_response / partner_writeback_from_converge

Do not invent partner fields. The allowlist is the hard writeback boundary.
"""

from __future__ import annotations

import re
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
# Odoo gate_mappers.EIE_STATE_COMPLETED — the only state it maps to status "ok".
ODOO_COMPLETED_STATE = "completed"

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


# An Odoo record reference: dotted model name, colon, integer id.
_ODOO_ENTITY_REF = re.compile(r"^[a-z][a-z0-9_.]*:\d+$")


def odoo_entity_ref(payload: dict[str, Any]) -> str | None:
    """Return the Odoo entity reference this payload carries, or None.

    Contract authority is the live builder, IB-Odoo_19
    ``plasticos_gate/services/gate_builders.py::build_converge_request``, which
    puts the identity ON the entity — ``entity["id"] = "res.partner:N"`` with
    ``entity["_odoo_entity_id"]`` as the compatibility alias — and never sends a
    top-level ``entity_id`` or ``entity_snapshot``. Matching on a top-level
    ``entity_id`` while excluding payloads that contain ``entity`` therefore
    rejected every real request.

    The reference pattern is the discriminator, so an internal EnrichRequest
    whose entity happens to carry a free-form ``id`` is not misrouted here.
    """
    if not isinstance(payload, dict):
        return None
    entity = payload.get("entity")
    if isinstance(entity, dict):
        for key in ("id", "_odoo_entity_id"):
            value = entity.get(key)
            if isinstance(value, str) and _ODOO_ENTITY_REF.match(value.strip()):
                return value.strip()
    # Pre-Gate internal shape: identity at the top level, no `entity` key.
    legacy = payload.get("entity_id")
    if isinstance(legacy, str) and legacy.strip() and "entity" not in payload:
        return legacy.strip()
    return None


def is_odoo_converge_payload(payload: dict[str, Any]) -> bool:
    """True when payload matches an Odoo Gate ConvergeRequest shape."""
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("entity_snapshot"), dict):
        return True
    return odoo_entity_ref(payload) is not None


def parse_odoo_converge_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Defensively parse Odoo ConvergeRequest.to_dict() into normalized fields."""
    if not isinstance(payload, dict):
        raise ValueError("converge payload must be a dict")

    entity_id = odoo_entity_ref(payload)
    if not entity_id:
        raise ValueError(
            "converge: entity identity is required — expected entity['id'] like "
            "'res.partner:55' (live Odoo builder) or a top-level entity_id"
        )

    domain_raw = payload.get("domain")
    # The live builder carries the domain in `object_type` (it is built as
    # `object_type=str(domain)`, e.g. "plasticos"). The pre-Gate shape has no
    # `entity` key and keeps the default instead of borrowing its object_type,
    # which there means a record type rather than a domain.
    if not (isinstance(domain_raw, str) and domain_raw.strip()) and isinstance(
        payload.get("entity"), dict
    ):
        domain_raw = payload.get("object_type")
    domain = (
        domain_raw.strip() if isinstance(domain_raw, str) and domain_raw.strip() else "plasticos"
    )

    snapshot_raw = payload.get("entity_snapshot")
    if not isinstance(snapshot_raw, dict):
        # Live builder: the partner snapshot IS `entity` (identity keys included,
        # filtered out below because they are not snapshot fields).
        snapshot_raw = payload.get("entity")
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

    # Live builder sends `max_variations` (clamped 1..10); pre-Gate sends max_passes.
    raw_passes = payload.get("max_passes")
    if raw_passes in (None, "", False):
        raw_passes = payload.get("max_variations")
    max_passes = _normalize_max_passes(raw_passes)

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
    """Build exactly the payload the live Odoo mapper consumes.

    Contract authority is IB-Odoo_19
    ``plasticos_gate/services/gate_mappers.py::map_converge_response``, which
    reads ``state``, ``failure_reason`` and ``fields`` off this payload (plus
    the metrics below) and derives its OWN ``status`` from
    ``state == "completed"``. It never reads ``status``, ``final_fields``, or
    ``writeback``; a payload carrying those instead leaves the mapper with
    ``state=None``, so it computes ``status="failed"`` and Odoo discards a
    perfectly good convergence.

    ``fields`` stays allowlist-filtered — that boundary is ours to enforce, and
    Odoo's ``partner_writeback_from_converge`` filters again on its side.
    """
    snapshot = parsed.get("entity_snapshot") or {}
    fields = filter_partner_fields(enrich_response.fields or {}, snapshot=snapshot)

    return {
        "run_id": run_id or new_run_id(),
        "state": enrich_response.state,
        "failure_reason": enrich_response.failure_reason,
        "fields": fields,
        "pass_count": int(enrich_response.pass_count or 0),
        "variation_count": enrich_response.variation_count,
        "confidence": enrich_response.confidence,
        "consensus_threshold": enrich_response.consensus_threshold,
        "uncertainty_score": enrich_response.uncertainty_score,
        "processing_time_ms": enrich_response.processing_time_ms,
        "quality_tier": enrich_response.quality_tier,
        "inference_version": enrich_response.inference_version,
        "kb_content_hash": enrich_response.kb_content_hash,
        "kb_files_consulted": list(enrich_response.kb_files_consulted or []),
        "kb_fragment_ids": list(enrich_response.kb_fragment_ids or []),
        "inferences": list(enrich_response.inferences or []),
        "grade_matches": list(enrich_response.grade_matches or []),
        "enrichment_payload": enrich_response.enrichment_payload,
        "feature_vector": enrich_response.feature_vector,
        "tokens_used": enrich_response.tokens_used,
    }


def build_odoo_converge_error(
    *,
    error: str,
    run_id: str | None = None,
    pass_count: int = 0,
    total_tokens: int = 0,
) -> dict[str, Any]:
    """Structured non-completed payload for graceful Odoo fallback.

    ``state`` is anything but "completed", so Odoo's mapper resolves
    ``status = failure_reason`` and ``_run_gate_converge`` fails closed to the
    operator-visible degraded state instead of marking the run injected.
    """
    return {
        "run_id": run_id or new_run_id(),
        "state": "failed",
        "failure_reason": error,
        "fields": {},
        "pass_count": pass_count,
        "tokens_used": total_tokens,
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
