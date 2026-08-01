"""
outcome_delegator.py
Pattern 3 — EvidenceRL/CRAG outcome-feedback loop: GRAPH rejection → ENRICH re-targeting

Purpose:
  Receives GRAPH scoring verdicts via OutcomeEvent (emitted by handle_outcome),
  converts REJECTED outcomes into targeted EnrichRequest objects with
  elevated consensus thresholds, and returns them for queue dispatch.
  Accepted / partial outcomes return None (no re-enrichment needed).

Dependencies:
  app.models.enrichment   — EnrichRequest
  app.models.events       — OutcomeEvent, OutcomeVerdict
  app.models.common       — EntityRef
  TransportPacket          — immutable I/O boundary

L9 Compliance:
  - No routes, auth, or rate-limiting
  - Idempotency key scoped to entity_id:run_id — safe to call multiple times
  - All outputs are new frozen objects
  - Zero stubs; all imports resolve
"""

from __future__ import annotations

from app.utils.safe_convert import safe_float
import hashlib
import logging
from typing import Any

from app.models.enrichment import EnrichRequest
from app.models.events import OutcomeEvent, OutcomeVerdict

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_ELEVATED_CONSENSUS_THRESHOLD: float = 0.80  # vs. standard 0.65
_ELEVATED_MAX_VARIATIONS: int = 4  # vs. standard 2–3
_CORRECTIVE_PASS_LABEL: str = "outcome_corrective"


# ── OutcomeEvent parsing ──────────────────────────────────────────────────────


def parse_outcome_payload(payload: dict[str, Any]) -> OutcomeEvent:
    """
    Boundary validator: converts the raw dict from handle_outcome into a typed
    OutcomeEvent.  Raises ValueError for malformed payloads so the handler can
    NACK the message rather than silently drop it.

    Integration point: app/handlers/handle_outcome.py
        event = parse_outcome_payload(packet.payload)
    """
    required_keys = {"entity_id", "run_id", "verdict", "failed_gates"}
    missing = required_keys - set(payload.keys())
    if missing:
        raise ValueError(f"OutcomeEvent payload missing required keys: {missing!r}")

    verdict_raw = payload["verdict"]
    try:
        verdict = OutcomeVerdict(verdict_raw)
    except ValueError as err:
        msg = (
            f"Unknown OutcomeVerdict value: {verdict_raw!r}. "
            f"Valid values: {[v.value for v in OutcomeVerdict]}"
        )
        raise ValueError(msg) from err

    return OutcomeEvent(
        entity_id=str(payload["entity_id"]),
        run_id=str(payload["run_id"]),
        verdict=verdict,
        failed_gates=list(payload.get("failed_gates", [])),
        confidence_deltas=dict(payload.get("confidence_deltas", {})),
        graph_score=safe_float(payload.get("graph_score", 0.0)),
        metadata=dict(payload.get("metadata", {})),
    )


# ── Idempotency ───────────────────────────────────────────────────────────────


def _build_idempotency_key(entity_id: str, run_id: str) -> str:
    """
    Deterministic idempotency key scoped to (entity_id, run_id).
    GRAPH can emit the same rejection event multiple times without
    producing duplicate EnrichRequests.
    """
    return hashlib.sha256(f"{entity_id}:{run_id}".encode()).hexdigest()


# ── Corrective request construction ──────────────────────────────────────────


def _select_target_fields(event: OutcomeEvent) -> list[str]:
    """
    Derive the enrichment target fields from the failed gates and the
    confidence_deltas map.  Fields with the largest negative delta are
    prioritised because they contribute most to the gate failures.

    Precedence:
      1. Fields explicitly named in failed_gates with format "gate:field_name"
      2. Fields in confidence_deltas with delta < 0, sorted by delta magnitude
    """
    targets: list[str] = []

    for gate_str in event.failed_gates:
        if ":" in gate_str:
            _, field_part = gate_str.split(":", 1)
            field_name = field_part.strip()
            if field_name and field_name not in targets:
                targets.append(field_name)

    # Supplement with confidence_deltas
    delta_sorted = sorted(
        [(f, d) for f, d in event.confidence_deltas.items() if d < 0],
        key=lambda x: x[1],  # most negative first
    )
    for field_name, _ in delta_sorted:
        if field_name not in targets:
            targets.append(field_name)

    return targets


def build_corrective_request(event: OutcomeEvent) -> EnrichRequest | None:
    """
    Convert a REJECTED OutcomeEvent into a targeted EnrichRequest.
    Returns None for accepted / partial verdicts.

    The returned EnrichRequest carries:
      - elevated consensus_threshold (0.80 vs. 0.65 standard)
      - elevated max_variations (4 vs. 2-3 standard)
      - pass_label = 'outcome_corrective' (visible in lineage trace)
      - idempotency_key scoped to entity_id:run_id
      - target_fields prioritised from failed_gates + confidence_deltas

    Integration point: app/handlers/handle_outcome.py
        request = build_corrective_request(event)
        if request:
            await task_queue.enqueue(request)
    """
    if event.verdict != OutcomeVerdict.REJECTED:
        logger.debug(
            "outcome_delegator.skip",
            extra={
                "entity_id": event.entity_id,
                "verdict": event.verdict.value,
            },
        )
        return None

    target_fields = _select_target_fields(event)
    idempotency_key = _build_idempotency_key(event.entity_id, event.run_id)

    logger.info(
        "outcome_delegator.corrective_request_built",
        extra={
            "entity_id": event.entity_id,
            "run_id": event.run_id,
            "target_fields": target_fields,
            "graph_score": event.graph_score,
            "failed_gates": event.failed_gates,
            "idempotency_key": idempotency_key,
        },
    )

    object_type = str(event.metadata.get("object_type", "unknown"))
    schema_map = dict.fromkeys(target_fields, "string") if target_fields else None
    entity_payload: dict[str, Any] = {
        "entity_id": event.entity_id,
        "_pass_label": _CORRECTIVE_PASS_LABEL,
        "_source_run_id": event.run_id,
        "_outcome_metadata": {
            "trigger": "graph_rejection",
            "graph_score": event.graph_score,
            "failed_gates": event.failed_gates,
            "confidence_deltas": event.confidence_deltas,
        },
    }
    return EnrichRequest(
        entity=entity_payload,
        object_type=object_type,
        objective=(
            f"Corrective enrichment after graph rejection (run {event.run_id}); "
            f"target fields: {', '.join(target_fields) if target_fields else '(none)'}"
        ),
        schema=schema_map,
        consensus_threshold=_ELEVATED_CONSENSUS_THRESHOLD,
        max_variations=_ELEVATED_MAX_VARIATIONS,
        idempotency_key=idempotency_key,
    )
