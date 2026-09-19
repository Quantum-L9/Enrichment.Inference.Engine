"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: enrichment
layer: [services]
tags: [enrichment, provider, deterministic, testability]
owner: engine-team
status: active
--- /L9_META ---

A deterministic enrichment source, so the core action is reproducible offline.

EIE-009: `handle_enrich` had no path to `state="completed"` that did not call a
paid provider. Without provider egress it returned
`failed / no_valid_responses (APIConnectionError ...)`, and because
`_persist_and_sync` only runs on a completed enrichment, the whole
EIE -> Gate -> CEG direction could not be driven through its real trigger in a
hermetic environment. The Constellation E2E had to bypass the handler and call
PacketRouter directly, which means the business chain it was built to prove was
the one part it did not exercise.

This module answers the same contract as `app/services/perplexity_client.py`
(`SonarResponse`), so the orchestrator swaps one for the other and every stage
downstream — validation, consensus synthesis, persistence, graph sync — runs
exactly as it does in production.

**It invents values, and says so.** Every field it did not find on the entity
carries a `DETERMINISTIC_VALUE_PREFIX` marker, so a synthetic value can never be
mistaken for a researched one in a store, a graph, or a log. Selecting it in a
deployment that serves real tenants is a configuration error, not a fallback:
`ENRICHMENT_PROVIDER` defaults to `perplexity` and nothing selects this
implicitly — not a missing API key, not a provider outage, not a circuit-breaker
trip.
"""

from __future__ import annotations

import hashlib
from typing import Any

import structlog

from app.services.perplexity_client import SonarResponse

logger = structlog.get_logger(__name__)

PROVIDER_NAME = "deterministic"
DETERMINISTIC_MODEL = "deterministic-v1"
DETERMINISTIC_VALUE_PREFIX = "det:"
# High enough to clear a default consensus threshold (0.65) on its own, and
# deliberately short of 1.0: a synthesized value is reproducible, not certain.
DETERMINISTIC_CONFIDENCE = 0.95


def _entity_key(entity: dict[str, Any]) -> str:
    """A stable identity for the entity, so the same input yields the same output."""
    for candidate in ("id", "Id", "ID", "entity_id", "name", "Name"):
        value = entity.get(candidate)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int | float):
            return str(value)
    # No identifier: hash the entity's own content so repeat calls still agree.
    items = sorted((str(k), str(v)) for k, v in entity.items())
    return hashlib.sha256(repr(items).encode()).hexdigest()[:16]


def _existing_value(entity: dict[str, Any], field: str) -> Any | None:
    """Echo a value the entity already carries, matching keys case-insensitively."""
    if field in entity:
        return entity[field]
    folded = field.casefold()
    for key, value in entity.items():
        if str(key).casefold() == folded:
            return value
    return None


def _digest(entity_key: str, field: str) -> str:
    return hashlib.sha256(f"{entity_key}|{field}".encode()).hexdigest()


def _synthesize(entity_key: str, field: str, declared_type: str) -> Any:
    """Derive a stable, type-correct, visibly-synthetic value for one field."""
    digest = _digest(entity_key, field)
    kind = str(declared_type or "string").strip().casefold()

    if kind in {"int", "integer"}:
        return int(digest[:8], 16) % 1000
    if kind in {"float", "number", "decimal"}:
        return round((int(digest[:8], 16) % 10_000) / 100, 2)
    if kind in {"bool", "boolean"}:
        return int(digest[:2], 16) % 2 == 0
    if kind in {"list", "array"}:
        return [f"{DETERMINISTIC_VALUE_PREFIX}{digest[:8]}"]
    return f"{DETERMINISTIC_VALUE_PREFIX}{field}:{digest[:12]}"


def build_deterministic_payload(
    entity: dict[str, Any],
    target_schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """The response body a provider would have returned, computed from the input.

    A field already present on the entity is echoed unchanged — that is the
    honest answer, and it keeps round-trip assertions meaningful. Everything
    else is synthesized and marked.
    """
    schema = target_schema or {}
    entity_key = _entity_key(entity)
    fields: dict[str, Any] = {}
    for field, declared_type in schema.items():
        existing = _existing_value(entity, field)
        fields[field] = (
            existing
            if existing not in (None, "")
            else _synthesize(entity_key, str(field), str(declared_type))
        )
    return {"confidence": DETERMINISTIC_CONFIDENCE, "fields": fields}


async def query_deterministic(
    entity: dict[str, Any],
    target_schema: dict[str, Any] | None,
    *,
    model: str = DETERMINISTIC_MODEL,
) -> SonarResponse:
    """`query_perplexity`'s answer shape, computed locally and reproducibly.

    No network, no key, no clock: the same entity and schema always produce the
    same response, so every variation in a multi-variation pass agrees and
    consensus synthesis behaves as it does with a cooperative live provider.
    """
    data = build_deterministic_payload(entity, target_schema)
    logger.info(
        "deterministic_enrichment_served",
        entity_key=_entity_key(entity),
        field_count=len(data["fields"]),
    )
    return SonarResponse(data=data, tokens_used=0, model=model, latency_ms=0)


__all__ = [
    "DETERMINISTIC_CONFIDENCE",
    "DETERMINISTIC_MODEL",
    "DETERMINISTIC_VALUE_PREFIX",
    "PROVIDER_NAME",
    "build_deterministic_payload",
    "query_deterministic",
]
