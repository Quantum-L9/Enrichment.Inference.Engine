"""
Pydantic v2 request/response schemas.

Contract guarantees:
  - EnrichRequest  is what Salesforce EnrichmentCalloutService sends.
  - EnrichRequest  is what Odoo async_executor sends.
  - EnrichResponse is what Salesforce EnrichmentWriteBack + RunLogger parse.
  - EnrichResponse is what Odoo enrichment_run stores.
  - BatchEnrichResponse wraps multiple for Odoo nightly cron batches.

Every field name and type is intentional. Do not rename without
updating both the Apex package and the Odoo bridge.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from typing import Any, cast

from pydantic import BaseModel, Field, field_validator

from app.engines.domain_yaml_reader import TENANT_ID_PATTERN, TENANT_ID_RESERVED_PREFIX

# ── Identity contract (operator decision 2026-10-22) ─────────
#
# tenant id == domain spec id == Neo4j database name. A tenant id is therefore
# a legal Neo4j database name: lowercase, starts with a letter, then letters,
# digits, dots or dashes, 3-63 characters, and never the reserved `system`
# prefix. Vertical templates are provenance only. A value that violates the
# grammar is rejected; nothing lowercases, trims or otherwise normalises it.
#
# The grammar has ONE owner: app/engines/domain_yaml_reader.py
# (`TENANT_ID_PATTERN`, `is_legal_tenant_id`; docs/contracts/DOMAIN_IDENTITY_REGISTRY.md).
# `validate_tenant_id` applies that same pattern and reserved prefix and only
# adds the reason a value was refused; it never restates the regex.

TENANT_ID_RE = TENANT_ID_PATTERN
_RESERVED_TENANT_PREFIX = TENANT_ID_RESERVED_PREFIX


def validate_tenant_id(value: Any) -> str:
    """Return ``value`` when it is a legal tenant id, else raise ValueError.

    Fail closed: no case folding, no whitespace stripping, no substitution.
    """
    if not isinstance(value, str):
        msg = f"tenant id must be a string, got {type(value).__name__}"
        raise ValueError(msg)
    if value != value.lower():
        msg = f"tenant id {value!r} must be lowercase"
        raise ValueError(msg)
    if not TENANT_ID_RE.fullmatch(value):
        msg = f"tenant id {value!r} is not a legal Neo4j database name ({TENANT_ID_RE.pattern})"
        raise ValueError(msg)
    if value.startswith(_RESERVED_TENANT_PREFIX):
        msg = f"tenant id {value!r} must not start with {_RESERVED_TENANT_PREFIX!r}"
        raise ValueError(msg)
    return value


# SEAM-02: the producer (CEG) sends an EnrichRequest body with a top-level
# `entity_id`, plus `entity.id`; older emitters carry only `entity.entity_id`.
# Identity resolves in exactly this order. There is no literal fallback: a body
# that names no identity has none, and the consumer rejects it.
ENTITY_IDENTITY_ORDER: tuple[str, ...] = ("entity_id", "entity.id", "entity.entity_id")


def resolve_entity_identity(payload: Mapping[str, Any]) -> str | None:
    """Resolve entity identity: top-level ``entity_id``, then ``entity.id``, then
    ``entity.entity_id``. Returns None when none of them carries a non-blank string."""
    top = payload.get("entity_id")
    if isinstance(top, str) and top.strip():
        return top.strip()
    entity = payload.get("entity")
    if isinstance(entity, Mapping):
        for key in ("id", "entity_id"):
            value = entity.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


# The converge consumer accepts one more carrier. The live Odoo builder
# (IB-Odoo_19 build_converge_request) puts the record reference on
# `entity._odoo_entity_id` beside `entity.id`, and older builders sent only the
# alias. It is the documented last fallback after the SEAM-02 order — the
# superset below is the whole converge grammar, resolved once per operation so
# the convergence loop, GraphReturnChannel and persistence key on one id.
CONVERGE_IDENTITY_ORDER: tuple[str, ...] = (*ENTITY_IDENTITY_ORDER, "entity._odoo_entity_id")


def resolve_converge_identity(payload: Mapping[str, Any]) -> str | None:
    """Resolve converge identity: the SEAM-02 order, then ``entity._odoo_entity_id``.

    Returns None when none of them carries a non-blank string."""
    resolved = resolve_entity_identity(payload)
    if resolved is not None:
        return resolved
    entity = payload.get("entity")
    if isinstance(entity, Mapping):
        value = entity.get("_odoo_entity_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# ── Request ──────────────────────────────────────────────────


# The field confidence threshold a requested field inherits when the
# consumer's field_thresholds map does not name it. ConvergenceConfig's
# confidence_threshold defaults to this value, so the standalone enrich path
# and the convergence loop share one default. It is a confidence bar, never
# the agreement bar (consensus_threshold, 0.65).
DEFAULT_FIELD_CONFIDENCE_THRESHOLD = 0.85

# Reserved response key; never a requested field.
_RESERVED_CONFIDENCE_KEY = "confidence"


def resolve_field_thresholds(
    requested_fields: Iterable[str],
    field_thresholds: Mapping[str, float] | None,
    default_threshold: float,
) -> dict[str, float] | None:
    """Resolve t_i for every requested field.

    t_i = field_thresholds[i] when the consumer named i, else ``default_threshold``.
    Returns None when the consumer sent no map: the absent-map contract is
    unchanged. Entries naming fields outside ``requested_fields`` are kept as
    sent, and the reserved ``confidence`` key is never given a bar.
    """
    if field_thresholds is None:
        return None
    resolved = {
        name: default_threshold for name in requested_fields if name != _RESERVED_CONFIDENCE_KEY
    }
    resolved.update(field_thresholds)
    return resolved


class EnrichRequest(BaseModel):
    """Single entity enrichment request.

    Callers:
      - Salesforce: EnrichmentCalloutService.cls → POST /api/v1/enrich
      - Odoo: async_executor.py → POST /api/v1/enrich
      - Clay: webhook → POST /api/v1/enrich
    """

    # The record's current field values
    entity: dict[str, Any] = Field(
        ...,
        description="Record fields as key-value pairs.",
    )

    # SEAM-02 producer envelope: identity and domain at the top level. Both are
    # optional on the HTTP surface (Salesforce/Odoo/Clay send neither); the SDK
    # `enrich` consumer requires an identity — see resolve_entity_identity().
    entity_id: str | None = Field(
        default=None,
        description="Entity identity; resolves before entity.id and entity.entity_id.",
    )
    domain: str | None = Field(
        default=None,
        description="Domain spec id (== tenant id under the identity contract).",
    )

    # What kind of record (Account, Lead, res.partner, etc)
    object_type: str = Field(
        ...,
        description="Source object API name.",
    )

    # What fields to enrich — JSON dict of {field_name: type_string}
    schema_: dict[str, str] | None = Field(
        default=None,
        validation_alias="schema",
        description="Target fields: {field_api_name: type}.",
    )

    # Natural language instruction
    objective: str = Field(
        ...,
        description="What the enrichment should accomplish.",
    )

    # KB domain selector (e.g., "HDPE", "PP", "recycling")
    kb_context: str | None = Field(
        default=None,
        description="KB profile identifier for selective injection.",
    )

    # Tuning knobs. consensus_threshold is the agreement bar. field_thresholds
    # is the consumer's minimum model confidence for named requested fields; a
    # map may name only some of them. A requested field the map omits inherits
    # the field confidence threshold (DEFAULT_FIELD_CONFIDENCE_THRESHOLD, or the
    # convergence loop's configured confidence_threshold). consensus_threshold
    # never substitutes for a field confidence threshold.
    consensus_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    field_thresholds: dict[str, float] | None = Field(default=None)
    max_variations: int = Field(default=5, ge=1, le=10)

    # Idempotency — caller can supply a UUID to prevent duplicate processing
    idempotency_key: str | None = Field(
        default=None,
        description="Caller-supplied UUID for dedup.",
    )

    @field_validator("schema_", mode="before")
    @classmethod
    def parse_schema_string(cls, v: Any) -> dict[str, str] | None:
        """Accept schema as JSON string (from Salesforce LongTextArea) or dict."""
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
        return cast("dict[str, str] | None", v)

    @field_validator("field_thresholds")
    @classmethod
    def thresholds_are_unit_interval(cls, value: Any) -> dict[str, float] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            msg = "field_thresholds must be an object of field name to confidence"
            raise ValueError(msg)
        cleaned: dict[str, float] = {}
        for key, raw in value.items():
            try:
                number = float(raw)
            except (TypeError, ValueError) as exc:
                msg = f"field_thresholds[{key!r}] is not a number"
                raise ValueError(msg) from exc
            if not math.isfinite(number) or number < 0.0 or number > 1.0:
                msg = f"field_thresholds[{key!r}] must be a finite number between 0 and 1"
                raise ValueError(msg)
            cleaned[str(key)] = number
        return cleaned


class BatchEnrichRequest(BaseModel):
    """Batch enrichment — up to 50 entities in one HTTP call."""

    entities: list[EnrichRequest] = Field(..., max_length=50)


# ── Response ─────────────────────────────────────────────────


class EnrichResponse(BaseModel):
    """Full enrichment response.

    Consumers:
      - Salesforce EnrichmentWriteBack reads `fields`
      - Salesforce EnrichmentRunLogger reads everything else
      - Odoo enrichment_run stores the entire response as JSON
    """

    # ── Enriched data (WriteBack reads this) ─────────
    fields: dict[str, Any] = Field(default_factory=dict)

    # ── Provenance (RunLogger reads these) ───────────
    confidence: float = 0.0
    kb_content_hash: str = ""
    variation_count: int = 0
    consensus_threshold: float = 0.0
    uncertainty_score: float = 0.0  # Aggregate uncertainty metric (0.0-10.0 scale)
    pass_count: int = 0  # Number of convergence passes executed
    inference_version: str = "v2.2.0"
    processing_time_ms: int = 0

    # ── Replay payloads ──────────────────────────────
    enrichment_payload: dict[str, Any] | None = None
    feature_vector: dict[str, Any] | None = None

    # ── KB audit trail ───────────────────────────────
    kb_files_consulted: list[str] = Field(default_factory=list)
    kb_fragment_ids: list[str] = Field(default_factory=list)

    # ── Inference results (from deterministic KB engine) ──
    inferences: list[dict[str, Any]] = Field(default_factory=list)
    quality_tier: str = "unknown"
    grade_matches: list[dict[str, Any]] = Field(default_factory=list)

    # ── Tokens (cost tracking) ───────────────────────
    tokens_used: int = 0

    # ── Error / state ────────────────────────────────
    failure_reason: str | None = None
    state: str = "completed"
    # Why a convergence loop stopped (F-018), e.g. "budget_exhausted",
    # "insufficient_improvement_delta_0.05", "max_passes_reached". None for a
    # single-pass enrich, which has no loop.
    convergence_reason: str | None = None


class BatchEnrichResponse(BaseModel):
    results: list[EnrichResponse]
    total: int
    succeeded: int
    failed: int
    total_processing_time_ms: int
    total_tokens_used: int = 0


class HealthCheckResponse(BaseModel):
    status: str = "ok"
    version: str = "2.3.0"
    kb_loaded: bool = False
    kb_polymers: int = 0
    kb_grades: int = 0
    kb_rules: int = 0
    circuit_breaker_state: str = "closed"
    # TASK-003: None = registration disabled/not attempted, True = accepted,
    # False = rejected/errored (health reports "degraded").
    gate_registered: bool | None = None
    # EIE-002: `gate_registered is None` cannot distinguish "registration is
    # switched off" from "registration was never attempted", and the second is a
    # node Gate cannot route to. This field names the state outright:
    #   registered    — Gate accepted this node
    #   failed        — Gate rejected it, or the attempt errored
    #   not_attempted — enabled, but no attempt has been recorded
    #   disabled      — switched off, or no gate_url configured
    # `failed` and `not_attempted` both degrade `status`.
    gate_registration: str = "disabled"


class ReadinessResponse(BaseModel):
    """GET /api/v1/ready — the routability signal a readiness probe reads.

    EIE-212-F003: /api/v1/health names a degraded registration in its body but
    always answers HTTP 200, and a Kubernetes readinessProbe reads the status
    code, not the body — so an unroutable pod was marked Ready. This response
    rides HTTP 503 whenever registration is enabled but `failed` or
    `not_attempted`, and HTTP 200 when `registered` or `disabled`. Liveness
    stays on /api/v1/health, so a Gate outage degrades routing without
    triggering restart loops.
    """

    ready: bool
    status: str  # "ready" | "not_ready"
    gate_registration: str
    version: str = "2.3.0"
