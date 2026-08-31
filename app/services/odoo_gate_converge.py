"""Compatibility adapter for the pre-canonical ``entity_snapshot`` converge dialect.

**This module is not the canonical converge contract.** The canonical rail is

    Odoo ``build_converge_request`` (EnrichRequest-shaped)
      -> Gate_SDK TransportPacket(action="converge")
      -> Constellation.Gate
      -> EIE ``handlers._handle_canonical_converge``
      -> ``EnrichResponse``

and it is served by ``EnrichRequest.model_validate(payload)`` directly, with no
translation. Everything here exists for one older shape — a top-level
``entity_id`` or an ``entity_snapshot`` dict — which the live Odoo producer
(IB-Odoo_19 ``plasticos_gate/services/gate_builders.py::build_converge_request``,
PR #163) has never emitted.

Why it must stay narrow: this adapter is lossy against a canonical payload. It
drops ``entity["id"]`` (not a snapshot key), replaces the caller's ``objective``
and ``object_type`` with its own, reinterprets ``max_variations`` as passes, and
truncates the response ``fields`` to the partner allowlist. Applying it to a
canonical request would silently rewrite the contract, so the discriminator
below matches the compatibility shapes *only* and can never claim a payload the
canonical branch should serve.

Do not invent partner fields. The allowlist is the hard writeback boundary.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog

from app.models.schemas import EnrichRequest, EnrichResponse
from app.services.request_deadline import CANONICAL_CONVERGE_BUDGET_SECONDS

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
# One budget SSOT for every converge branch — see request_deadline.
DEFAULT_CONVERGE_TIMEOUT_SECONDS = CANONICAL_CONVERGE_BUDGET_SECONDS
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
    """Return the compatibility-shape entity reference, or None.

    Only the pre-canonical shapes are recognised: identity at the TOP level
    (``entity_id``), which the canonical EnrichRequest has no field for.

    Deliberately NOT matched: ``entity["id"]`` / ``entity["_odoo_entity_id"]``.
    That is where the live Odoo builder puts the canonical identity, and it is
    exactly the payload the canonical branch must serve. Matching it here is
    what routed every production request into this lossy adapter (see the
    module docstring); the canonical branch keeps ``entity`` verbatim, so the
    identity survives there rather than being re-derived here.
    """
    if not isinstance(payload, dict):
        return None
    # Canonical precedence: a payload carrying an `entity` dict IS an
    # EnrichRequest — that field is required by the canonical model and absent
    # from this dialect. Pydantic ignores an extra top-level `entity_id`, so
    # without this guard a canonical request that happened to carry one would be
    # diverted here and silently rewritten, which is the exact lossy routing the
    # narrowed discriminator exists to prevent (EIE18).
    if isinstance(payload.get("entity"), dict):
        return None
    legacy = payload.get("entity_id")
    if isinstance(legacy, str) and legacy.strip():
        return legacy.strip()
    snapshot = payload.get("entity_snapshot")
    if isinstance(snapshot, dict):
        for key in ("id", "_odoo_entity_id"):
            value = snapshot.get(key)
            if isinstance(value, str) and _ODOO_ENTITY_REF.match(value.strip()):
                return value.strip()
    return None


def is_odoo_compat_converge_payload(payload: dict[str, Any]) -> bool:
    """True only for the pre-canonical ``entity_snapshot`` / ``entity_id`` dialect.

    A canonical EnrichRequest — ``entity`` + ``object_type`` + ``objective``,
    identity on ``entity["id"]`` — returns False here and is served by the
    canonical branch untranslated. There is exactly one canonical contract
    (EIE19); this predicate is the boundary that keeps it that way.
    """
    if not isinstance(payload, dict):
        return False
    # Canonical wins whenever `entity` is present, even alongside a stray
    # `entity_snapshot` or top-level `entity_id`.
    if isinstance(payload.get("entity"), dict):
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
            "converge (compatibility dialect): a top-level entity_id is required, "
            "e.g. 'res.partner:55'"
        )

    domain_raw = payload.get("domain")
    # In this dialect `object_type` means a record type, not a domain, so it is
    # never borrowed as one. (Canonical payloads, where `object_type` DOES carry
    # the domain, are resolved on the canonical branch and never arrive here.)
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
