"""Tier-2 contract probe: unlock_map prioritization wiring is imported and callable.

Full behavioral coverage lives in tests/test_convergence_controller.py.
This file stays tiny on purpose so New Code CPD stays under the Sonar gate.
"""

from __future__ import annotations

import pytest

from app.engines.inference_bridge_v2 import prioritize_search_targets

pytestmark = [pytest.mark.enforcement]


def test_contract_prioritize_search_targets_reorders_only() -> None:
    targets = ["a", "b", "c"]
    unlock = {"c": 3.0, "a": 1.0, "b": 2.0}
    out = prioritize_search_targets(targets, unlock, None)
    assert out == ["c", "b", "a"]
    assert set(out) == set(targets)
