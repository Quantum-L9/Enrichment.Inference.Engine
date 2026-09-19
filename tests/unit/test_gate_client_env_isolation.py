"""EIE-005 — building a Gate client must not pin GATE_URL for the process.

``build_gate_client_config`` used to write ``os.environ["GATE_URL"]`` when the
variable was unset, because the SDK's env factory raises without it. The write
was permanent: the first client constructed fixed GATE_URL for every later
caller, and the mutation was invisible to anything reading configuration.
"""

from __future__ import annotations

import os

import pytest

from app.services.gate_client import EIE_NODE_NAME, build_gate_client_config


@pytest.fixture(autouse=True)
def _clean_gate_url(monkeypatch):
    # No yield: nothing needs to run after the test, and monkeypatch undoes both
    # changes itself at teardown. A bare trailing `yield` only made this look
    # like a fixture with cleanup it does not have (SonarQube S9100).
    monkeypatch.delenv("GATE_URL", raising=False)
    monkeypatch.setenv("L9_NODE_NAME", EIE_NODE_NAME)


def test_building_a_client_leaves_no_gate_url_behind() -> None:
    assert "GATE_URL" not in os.environ
    config = build_gate_client_config("https://gate-a.test", timeout_seconds=5.0)
    assert config.gate_url == "https://gate-a.test"
    assert "GATE_URL" not in os.environ, "GATE_URL outlived the call that needed it"


def test_a_second_caller_gets_its_own_gate() -> None:
    """The defect: the second URL used to be silently replaced by the first."""
    first = build_gate_client_config("https://gate-a.test", timeout_seconds=5.0)
    second = build_gate_client_config("https://gate-b.test", timeout_seconds=5.0)
    assert first.gate_url == "https://gate-a.test"
    assert second.gate_url == "https://gate-b.test"


def test_an_exported_gate_url_is_preserved_exactly(monkeypatch) -> None:
    monkeypatch.setenv("GATE_URL", "https://gate-from-env.test")
    config = build_gate_client_config("https://gate-arg.test", timeout_seconds=5.0)
    # The argument still wins for the client itself...
    assert config.gate_url == "https://gate-arg.test"
    # ...and the operator's exported value is untouched.
    assert os.environ["GATE_URL"] == "https://gate-from-env.test"


def test_trailing_slash_is_normalised() -> None:
    config = build_gate_client_config("https://gate-a.test/", timeout_seconds=5.0)
    assert config.gate_url == "https://gate-a.test"


def test_empty_gate_url_is_refused() -> None:
    with pytest.raises(ValueError, match="gate_url must be configured"):
        build_gate_client_config("   ", timeout_seconds=5.0)
    assert "GATE_URL" not in os.environ


def test_an_exception_inside_the_window_still_propagates() -> None:
    """Caught by ruff B012: a `return` in the finally block silenced whatever
    was raised inside the window — including the SDK's own
    ValueError("GATE_URL is required"), which the caller must see."""
    from app.services.gate_client import _gate_url_visible_to_sdk

    with pytest.raises(RuntimeError, match="boom"), _gate_url_visible_to_sdk("https://g.test"):
        raise RuntimeError("boom")

    assert "GATE_URL" not in os.environ, "the window must close even on failure"


def test_an_exported_value_survives_an_exception(monkeypatch) -> None:
    from app.services.gate_client import _gate_url_visible_to_sdk

    monkeypatch.setenv("GATE_URL", "https://operator.test")
    with pytest.raises(RuntimeError), _gate_url_visible_to_sdk("https://other.test"):
        raise RuntimeError("boom")

    assert os.environ["GATE_URL"] == "https://operator.test"
