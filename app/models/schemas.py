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
from typing import Any

from pydantic import BaseModel, Field, field_validator

# ── Request ──────────────────────────────────────────────────


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

    # Tuning knobs
    consensus_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
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
        return v


class BatchEnrichRequest(BaseModel):
    """Batch enrichment — up to 50 entities in one HTTP call."""

    entities: list[EnrichRequest] = Field(..., max_length=50)


# ── Response ─────────────────────────────────────────────────

# The single terminal state that means "this convergence produced a durable,
# usable result". Consumers derive their own status from it — IB-Odoo_19
# `gate_mappers.map_converge_response` maps `state == COMPLETED_STATE` to
# status "ok" and anything else to its degraded path. It is a canonical
# enrichment-domain constant, not an Odoo-specific one.
COMPLETED_STATE = "completed"


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
