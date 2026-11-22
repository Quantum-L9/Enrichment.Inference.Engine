"""tests/e2e/gate_loop is opt-in: collected only when EIE_E2E_GATE_LOOP=1.

The rail (Gate + EIE + CEG + Neo4j + Postgres as separate processes in Docker,
ed25519-signed) does not exist on a normal unit run. Without the flag nothing
here is collected, so default CI neither runs it nor reports it as skipped.
With the flag a missing rail is a failure, never a skip.
"""

from __future__ import annotations

import os

import pytest

ENABLED = os.environ.get("EIE_E2E_GATE_LOOP", "").strip() == "1"
collect_ignore_glob = (
    [] if ENABLED else ["test_*.py", "rail.py", "driver/*", "probes/*", "scripts/*"]
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "gate_loop: real-process signed EIE+Gate+CEG+Neo4j rail (opt-in)"
    )
