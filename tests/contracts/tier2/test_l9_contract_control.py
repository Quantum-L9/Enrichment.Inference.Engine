from __future__ import annotations

import json
from typing import Any

import pytest

from scripts.l9_contract_control import (
    build_review_signal,
    select_gates,
    verify_attestation,
    verify_constitution,
)

pytestmark = [pytest.mark.unit, pytest.mark.enforcement]


def test_verify_constitution_passes_for_current_pack() -> None:
    ok, errors = verify_constitution()
    assert ok, errors


def test_verify_attestation_passes_for_current_runtime_surface() -> None:
    ok, errors = verify_attestation()
    assert ok, errors


def test_select_gates_for_api_change_includes_behavior_gate() -> None:
    selection = select_gates(
        [
            "app/api/v1/discover.py",
            "docs/contracts/api/openapi.yaml",
            "tests/contracts/tier2/test_enforcement_behavior.py",
        ]
    )
    assert "constitution_verify" in selection.ids
    assert "tier2_behavior" in selection.ids


def test_select_gates_for_packet_change_includes_packet_gate() -> None:
    selection = select_gates(
        [
            "chassis/envelope.py",
            "docs/contracts/agents/protocols/packet-envelope.yaml",
        ]
    )
    assert "tier2_packet" in selection.ids


def test_select_gates_for_event_change_includes_event_gate() -> None:
    selection = select_gates(
        [
            "app/services/event_emitter.py",
            "docs/contracts/events/asyncapi.yaml",
        ]
    )
    assert "tier2_events" in selection.ids


def test_review_signal_blocks_contract_bound_change_without_companion() -> None:
    ok, markdown = build_review_signal(["app/api/v1/chassis_endpoint.py"])
    assert ok is False
    assert "Blocking finding" in markdown


def test_review_signal_passes_with_companion_contract_and_tests() -> None:
    ok, markdown = build_review_signal(
        [
            "app/api/v1/chassis_endpoint.py",
            "docs/contracts/api/openapi.yaml",
            "docs/contracts/node.constitution.yaml",
            "tests/contracts/tier2/test_enforcement_packet_runtime.py",
        ]
    )
    assert ok is True
    assert "Surface checks" in markdown


def test_select_gates_output_shape_is_json_serializable() -> None:
    selection = select_gates(["docs/contracts/events/asyncapi.yaml"])
    payload: dict[str, Any] = {"gate_ids": selection.ids, "commands": selection.commands}
    json.dumps(payload)


def test_verify_attestation_fails_closed_when_app_is_unimportable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WU-23 / F-029 negative control: an attestation that cannot be built is a FAIL."""
    import sys

    from scripts import l9_contract_control

    monkeypatch.setitem(sys.modules, "app.services.runtime_attestation", None)
    ok, errors = verify_attestation()
    assert ok is False
    assert errors

    monkeypatch.setattr(sys, "argv", ["l9_contract_control.py", "verify-attestation"])
    assert l9_contract_control.main() == 1
