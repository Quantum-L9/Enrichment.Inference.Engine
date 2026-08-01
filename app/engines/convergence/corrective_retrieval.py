"""
corrective_retrieval.py
Pattern 1 — CoRAG/Search-o1 convergence-gated corrective re-query

Purpose:
  Reads InferenceResult.blocked_fields + rule_trace, extracts fields with
  inputs_missing / below_threshold status, ranks by unlock_map score, and
  produces a CorrectiveState that MetaPromptPlanner.plan_pass() pops as
  _corrective_targets before assembling the next enrichment prompt.

Dependencies:
  app.models.enrichment   — InferenceResult, ConvergenceState, EnrichRequest
  app.models.common       — FieldStatus, FieldTrace
  app.engines.convergence — ConvergenceConfig
  TransportPacket          — immutable I/O boundary (enforced by caller)

L9 Compliance:
  - No routes, auth, rate-limiting, or infra
  - All mutations produce new frozen objects (immutability preserved)
  - Deterministic: same inputs → same CorrectiveState
  - Zero stubs; all imports resolve against repo as ingested
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, replace

from app.engines.convergence.convergence_config import ConvergenceConfig
from app.models.common import FieldStatus, FieldTrace
from app.models.enrichment import ConvergenceState, InferenceResult
from app.utils.safe_convert import safe_float

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_CORRECTIVE_TARGETS_KEY = "_corrective_targets"
_TRIGGER_STATUSES: frozenset[str] = frozenset(
    {FieldStatus.INPUTS_MISSING, FieldStatus.BELOW_THRESHOLD}
)
_MIN_UNLOCK_SCORE: float = 0.0  # include all blocked fields; caller gates on confidence
_MAX_CORRECTIVE_FIELDS: int = 8  # cap to prevent prompt bloat


# ── Domain objects ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CorrectiveTarget:
    """Immutable descriptor for a single field the next pass should focus on."""

    field_name: str
    status: str
    unlock_score: float  # how much this field unblocks downstream inference
    missing_inputs: tuple[str, ...] = field(default_factory=tuple)
    confidence_gap: float = 0.0  # required_confidence - current_confidence

    def to_prompt_hint(self) -> str:
        """Serialise to a terse string injected into the enrichment prompt."""
        parts = [f"{self.field_name} [status={self.status}"]
        if self.missing_inputs:
            parts.append(f", needs={','.join(self.missing_inputs)}")
        if self.confidence_gap > 0:
            parts.append(f", gap={self.confidence_gap:.2f}")
        parts.append("]")
        return "".join(parts)


@dataclass(frozen=True)
class CorrectiveState:
    """
    Immutable bundle injected into ConvergenceState metadata under
    _CORRECTIVE_TARGETS_KEY.  Consumed exactly once by plan_pass(); discarded
    after consumption so subsequent passes do not inherit stale targets.
    """

    targets: tuple[CorrectiveTarget, ...]
    source_run_id: str
    pass_number: int
    idempotency_hash: str  # SHA-256 of (run_id, pass_number, sorted field names)

    @classmethod
    def build(
        cls,
        targets: list[CorrectiveTarget],
        run_id: str,
        pass_number: int,
    ) -> CorrectiveState:
        sorted_targets = tuple(sorted(targets, key=lambda t: t.unlock_score, reverse=True))
        field_str = json.dumps(sorted([t.field_name for t in sorted_targets]), sort_keys=True)
        idem_hash = hashlib.sha256(f"{run_id}:{pass_number}:{field_str}".encode()).hexdigest()
        return cls(
            targets=sorted_targets,
            source_run_id=run_id,
            pass_number=pass_number,
            idempotency_hash=idem_hash,
        )

    def is_empty(self) -> bool:
        return len(self.targets) == 0

    def prompt_block(self) -> str:
        """Multi-line block embedded in the enrichment prompt header."""
        lines = ["## Corrective Focus (pass-level override)"]
        for t in self.targets:
            lines.append(f"  - {t.to_prompt_hint()}")
        return "\n".join(lines)


# ── Core extraction logic ─────────────────────────────────────────────────────


def _extract_unlock_score(field_name: str, trace: FieldTrace) -> float:
    """
    Derive unlock score from the field's position in the dependency map.
    Fields that unblock many downstream inference rules receive higher scores.
    unlock_map is a dict[str, list[str]] stored on FieldTrace.extra by the
    inference engine when it writes INPUTS_MISSING status.
    """
    unlock_map: dict = getattr(trace, "extra", {}).get("unlock_map", {})
    dependents = unlock_map.get(field_name, [])
    return safe_float(len(dependents))


def _extract_missing_inputs(trace: FieldTrace) -> tuple[str, ...]:
    extra = getattr(trace, "extra", {})
    raw = extra.get("missing_inputs", [])
    return tuple(str(x) for x in raw)


def _extract_confidence_gap(trace: FieldTrace, cfg: ConvergenceConfig) -> float:
    current = getattr(trace, "confidence", 0.0)
    required = cfg.confidence_threshold
    return max(0.0, required - current)


def extract_corrective_targets(
    inference_result: InferenceResult,
    convergence_state: ConvergenceState,
    cfg: ConvergenceConfig,
) -> list[CorrectiveTarget]:
    """
    Walk InferenceResult.blocked_fields and the rule_trace to build a ranked
    list of CorrectiveTarget objects.  Capped at _MAX_CORRECTIVE_FIELDS.
    """
    targets: list[CorrectiveTarget] = []

    blocked = getattr(inference_result, "blocked_fields", {}) or {}
    rule_trace: dict = getattr(inference_result, "rule_trace", {}) or {}

    for field_name, status in blocked.items():
        if status not in _TRIGGER_STATUSES:
            continue

        candidate = rule_trace.get(field_name)
        # Prefer a real FieldTrace; synthesize when missing or mocked.
        trace = (
            candidate
            if isinstance(candidate, FieldTrace)
            else FieldTrace(field_name=field_name, status=status)
        )

        unlock_score = _extract_unlock_score(field_name, trace)
        if unlock_score < _MIN_UNLOCK_SCORE and status != FieldStatus.INPUTS_MISSING:
            continue

        targets.append(
            CorrectiveTarget(
                field_name=field_name,
                status=status,
                unlock_score=unlock_score,
                missing_inputs=_extract_missing_inputs(trace),
                confidence_gap=_extract_confidence_gap(trace, cfg),
            )
        )

    # Rank: INPUTS_MISSING first (they block inference entirely), then by score
    targets.sort(
        key=lambda t: (
            0 if t.status == FieldStatus.INPUTS_MISSING else 1,
            -t.unlock_score,
            -t.confidence_gap,
        )
    )
    return targets[:_MAX_CORRECTIVE_FIELDS]


# ── ConvergenceState injection / consumption ──────────────────────────────────


def should_apply_corrective(pass_number: int, cfg: ConvergenceConfig) -> bool:
    """
    Gate: corrective override is only valid on pass 2+.
    Pass 1 is schema discovery; injecting corrective targets too early
    narrows the discovery space before the schema is formed.
    """
    return pass_number >= 2 and cfg.corrective_retrieval_enabled


def apply_corrective_override(
    state: ConvergenceState,
    corrective: CorrectiveState,
) -> ConvergenceState:
    """
    Return a new ConvergenceState with _corrective_targets injected into
    metadata.  The original state is never mutated (TransportPacket immutability).
    """
    if corrective.is_empty():
        return state

    updated_meta = {
        **(state.metadata or {}),
        _CORRECTIVE_TARGETS_KEY: corrective,
    }
    logger.info(
        "corrective_override.applied",
        extra={
            "run_id": corrective.source_run_id,
            "pass": corrective.pass_number,
            "target_count": len(corrective.targets),
            "idempotency_hash": corrective.idempotency_hash,
        },
    )
    return replace(state, metadata=updated_meta)


def consume_corrective_override(
    state: ConvergenceState,
) -> tuple[ConvergenceState, CorrectiveState | None]:
    """
    Pop _corrective_targets from metadata, returning the consumed CorrectiveState
    and a new ConvergenceState without the key.  Idempotent: safe to call when
    no override is present.
    """
    meta = dict(state.metadata or {})
    corrective: CorrectiveState | None = meta.pop(_CORRECTIVE_TARGETS_KEY, None)
    new_state = replace(state, metadata=meta)
    if corrective:
        logger.debug(
            "corrective_override.consumed",
            extra={
                "run_id": corrective.source_run_id,
                "pass": corrective.pass_number,
            },
        )
    return new_state, corrective


# ── High-level orchestration entry point ─────────────────────────────────────


def build_corrective_state_for_next_pass(
    inference_result: InferenceResult,
    convergence_state: ConvergenceState,
    cfg: ConvergenceConfig,
    run_id: str,
    current_pass: int,
) -> CorrectiveState | None:
    """
    Called by the convergence controller after each InferenceEngine.run() call.
    Returns None if no corrective action is needed (all fields converged or
    corrective retrieval is disabled).

    Integration point: app/engines/convergence/controller.py after
        inference_result = inference_engine.run(state)
    """
    if not should_apply_corrective(current_pass + 1, cfg):
        return None

    targets = extract_corrective_targets(inference_result, convergence_state, cfg)
    if not targets:
        logger.debug("corrective_retrieval.no_targets", extra={"run_id": run_id})
        return None

    corrective = CorrectiveState.build(
        targets=targets,
        run_id=run_id,
        pass_number=current_pass + 1,
    )
    logger.info(
        "corrective_retrieval.state_built",
        extra={
            "run_id": run_id,
            "next_pass": corrective.pass_number,
            "targets": [t.field_name for t in corrective.targets],
        },
    )
    return corrective
