"""One budget hierarchy: Gate bounds the packet, EIE caps within it.

Gate PR #14 writes the *bounded remaining* budget of the operation into the
dispatch packet's ``header.timeout_ms``; the Gate_SDK worker runtime bounds the
handler with that same number. EIE's own 25 s complete-operation ceiling is a
cap inside that budget, never a floor beneath it.

The defect these tests close: ``Deadline.start(25)`` unconditionally, which
started a fresh 25 s operation regardless of how little Gate said was left.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.update(
    {
        "PERPLEXITY_API_KEY": "test-key",
        "API_SECRET_KEY": "test-secret-key-32-chars-long!!",
        "KB_DIR": "./kb",
    }
)

import pytest

from app.services.request_deadline import (
    CANONICAL_CONVERGE_BUDGET_SECONDS,
    RESPONSE_RESERVE_SECONDS,
    Deadline,
    deadline_scope,
    effective_budget_seconds,
    packet_timeout_ms,
    provider_attempt_timeout,
)


def _packet(timeout_ms: int | None):
    """A stand-in with the one field EIE reads off a transport packet."""
    return SimpleNamespace(header=SimpleNamespace(timeout_ms=timeout_ms))


# --------------------------------------------------------------------------
# §13 — the three required cases
# --------------------------------------------------------------------------


def test_packet_30s_gives_eie_its_own_25s_ceiling():
    """A budget larger than EIE's ceiling does not raise EIE's ceiling."""
    assert effective_budget_seconds(30_000) == 25.0 == CANONICAL_CONVERGE_BUDGET_SECONDS


def test_packet_12s_caps_eie_at_12s():
    """A budget smaller than the ceiling binds: EIE gets 12 s, not 25 s."""
    assert effective_budget_seconds(12_000) == 12.0


def test_packet_2s_cannot_start_a_fresh_25s_operation():
    """~2 s remaining leaves no room for work once the response reserve is held."""
    budget = effective_budget_seconds(2_000)
    assert budget == 2.0
    assert budget < CANONICAL_CONVERGE_BUDGET_SECONDS

    deadline = Deadline.start(budget)
    assert deadline.expired(), "a 2 s budget must leave no usable work time"
    assert deadline.attempt_timeout() is None, "no provider attempt may start"


# --------------------------------------------------------------------------
# The general rule and its edges
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("packet_ms", "expected"),
    [
        (None, 25.0),  # no packet: no upstream bound, ceiling stands alone
        (60_000, 25.0),  # Gate generous: ceiling still binds
        (30_000, 25.0),
        (25_000, 25.0),  # exactly equal
        (24_999, 24.999),  # one ms under: packet binds
        (12_000, 12.0),
        (2_000, 2.0),
        (1, 0.001),
        (0, 0.0),  # nothing left
        (-5_000, 0.0),  # already overdue: never negative, never a fresh start
    ],
)
def test_effective_budget_is_min_of_ceiling_and_packet(packet_ms, expected):
    assert effective_budget_seconds(packet_ms) == pytest.approx(expected)


def test_budget_never_exceeds_the_packet_budget():
    """The invariant Gate depends on: EIE never promises past Gate's own wait."""
    for packet_ms in range(500, 40_000, 500):
        assert effective_budget_seconds(packet_ms) <= packet_ms / 1000.0


def test_budget_never_exceeds_the_eie_ceiling():
    for packet_ms in list(range(500, 90_000, 500)) + [None]:
        assert effective_budget_seconds(packet_ms) <= CANONICAL_CONVERGE_BUDGET_SECONDS


# --------------------------------------------------------------------------
# Reading the budget off a real packet shape
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("packet", "expected"),
    [
        (None, None),  # direct in-process caller
        (_packet(12_000), 12_000),
        (_packet(None), None),  # header present, field unset
        (_packet(0), None),  # non-positive is not a budget
        (_packet(-1), None),
        (SimpleNamespace(), None),  # no header at all
    ],
)
def test_packet_timeout_ms_reads_defensively(packet, expected):
    assert packet_timeout_ms(packet) == expected


def test_real_sdk_packet_budget_is_read():
    """Against a genuine SDK TransportPacket, not a stand-in."""
    from constellation_node_sdk import create_transport_packet

    packet = create_transport_packet(
        action="converge",
        payload={"entity": {"id": "res.partner:1"}},
        source_node="gate",
        destination_node="enrichment-engine",
        tenant="acme",
        timeout_ms=12_000,
    )
    assert packet_timeout_ms(packet) == 12_000
    assert effective_budget_seconds(packet_timeout_ms(packet)) == 12.0


# --------------------------------------------------------------------------
# The budget actually governs provider attempts
# --------------------------------------------------------------------------


def test_provider_attempt_draws_from_the_reduced_budget():
    """A provider attempt under a 12 s packet may not claim the 20 s cap."""
    with deadline_scope(Deadline.start(effective_budget_seconds(12_000))):
        timeout = provider_attempt_timeout(fallback_seconds=30.0)
    assert timeout is not None
    assert timeout <= 12.0 - RESPONSE_RESERVE_SECONDS + 0.01
    assert timeout == pytest.approx(10.0, abs=0.05)


def test_provider_attempt_refused_under_an_almost_expired_packet():
    with deadline_scope(Deadline.start(effective_budget_seconds(2_000))):
        assert provider_attempt_timeout(fallback_seconds=30.0) is None


def test_provider_attempt_uses_full_cap_when_the_ceiling_binds():
    with deadline_scope(Deadline.start(effective_budget_seconds(30_000))):
        timeout = provider_attempt_timeout(fallback_seconds=30.0)
    assert timeout == pytest.approx(20.0, abs=0.05)  # PROVIDER_ATTEMPT_CAP_SECONDS
