"""
test_outcome_delegator.py — 20 deterministic test cases
"""

import math
from unittest.mock import MagicMock

import pytest

from app.models.events import OutcomeEvent, OutcomeVerdict
from app.services.outcome_delegator import (
    _CORRECTIVE_PASS_LABEL,
    _ELEVATED_CONSENSUS_THRESHOLD,
    _ELEVATED_MAX_VARIATIONS,
    _build_idempotency_key,
    _select_target_fields,
    build_corrective_request,
    parse_outcome_payload,
)


def _event(verdict=OutcomeVerdict.REJECTED, failed_gates=None, deltas=None):
    ev = MagicMock(spec=OutcomeEvent)
    ev.entity_id = "ent-001"
    ev.run_id = "run-abc"
    ev.verdict = verdict
    ev.failed_gates = ["gate:material_grade"] if failed_gates is None else failed_gates
    ev.confidence_deltas = {"material_grade": -0.30} if deltas is None else deltas
    ev.graph_score = 0.42
    ev.metadata = {}
    return ev


# ── parse_outcome_payload ─────────────────────────────────────────────────────


def test_parse_valid_payload():
    payload = {
        "entity_id": "e1",
        "run_id": "r1",
        "verdict": "rejected",
        "failed_gates": [],
    }
    event = parse_outcome_payload(payload)
    assert event.verdict == OutcomeVerdict.REJECTED


def test_parse_missing_key_raises():
    with pytest.raises(ValueError, match="missing required keys"):
        parse_outcome_payload({"entity_id": "x"})


def test_parse_invalid_verdict_raises():
    payload = {"entity_id": "e", "run_id": "r", "verdict": "nonsense", "failed_gates": []}
    with pytest.raises(ValueError, match="Unknown OutcomeVerdict"):
        parse_outcome_payload(payload)


def test_parse_accepted_verdict():
    payload = {
        "entity_id": "e1",
        "run_id": "r1",
        "verdict": "accepted",
        "failed_gates": [],
    }
    event = parse_outcome_payload(payload)
    assert event.verdict == OutcomeVerdict.ACCEPTED


def test_parse_optional_fields_default():
    payload = {"entity_id": "e", "run_id": "r", "verdict": "accepted", "failed_gates": []}
    event = parse_outcome_payload(payload)
    assert event.confidence_deltas == {}
    assert math.isclose(event.graph_score, 0.0, abs_tol=1e-9)


# ── _select_target_fields ─────────────────────────────────────────────────────


def test_gate_colon_parse():
    ev = _event(failed_gates=["gate:hdpe_grade", "gate:mfi_range"])
    fields = _select_target_fields(ev)
    assert "hdpe_grade" in fields
    assert "mfi_range" in fields


def test_gate_no_colon_ignored():
    ev = _event(failed_gates=["raw_gate_no_colon"], deltas={})
    fields = _select_target_fields(ev)
    assert fields == []


def test_delta_negative_included():
    ev = _event(failed_gates=[], deltas={"a": -0.3, "b": 0.1, "c": -0.5})
    fields = _select_target_fields(ev)
    assert "a" in fields and "c" in fields
    assert "b" not in fields


def test_delta_ordering_most_negative_first():
    ev = _event(failed_gates=[], deltas={"a": -0.1, "b": -0.9})
    fields = _select_target_fields(ev)
    assert fields[0] == "b"


def test_no_duplicates():
    ev = _event(
        failed_gates=["gate:hdpe_grade"],
        deltas={"hdpe_grade": -0.4},
    )
    fields = _select_target_fields(ev)
    assert fields.count("hdpe_grade") == 1


# ── build_corrective_request ──────────────────────────────────────────────────


def test_rejected_produces_request():
    ev = _event(OutcomeVerdict.REJECTED)
    req = build_corrective_request(ev)
    assert req is not None


def test_accepted_returns_none():
    ev = _event(OutcomeVerdict.ACCEPTED)
    assert build_corrective_request(ev) is None


def test_partial_returns_none():
    ev = _event(OutcomeVerdict.PARTIAL)
    assert build_corrective_request(ev) is None


def test_elevated_consensus_threshold():
    req = build_corrective_request(_event())
    assert req.consensus_threshold == _ELEVATED_CONSENSUS_THRESHOLD


def test_elevated_max_variations():
    req = build_corrective_request(_event())
    assert req.max_variations == _ELEVATED_MAX_VARIATIONS


def test_pass_label_set():
    req = build_corrective_request(_event())
    assert req.entity["_pass_label"] == _CORRECTIVE_PASS_LABEL


def test_idempotency_key_deterministic():
    ev = _event()
    k1 = _build_idempotency_key(ev.entity_id, ev.run_id)
    k2 = _build_idempotency_key(ev.entity_id, ev.run_id)
    assert k1 == k2


def test_idempotency_key_differs_by_run():
    k1 = _build_idempotency_key("e1", "run-1")
    k2 = _build_idempotency_key("e1", "run-2")
    assert k1 != k2


def test_metadata_includes_graph_score():
    req = build_corrective_request(_event(deltas={"f": -0.2}))
    assert "graph_score" in req.entity["_outcome_metadata"]


def test_source_run_id_propagated():
    ev = _event()
    req = build_corrective_request(ev)
    assert req.entity["_source_run_id"] == ev.run_id
