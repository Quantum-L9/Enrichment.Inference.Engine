"""Opt-in pytest entry for the gate_loop rail (see README.md).

The rail runs once per session for the scenarios named in GL_CASES (default:
all of them). Each case id those scenarios produce becomes its own test; the
mapping is rail.CASE_IDS. A case passes only when the rail recorded PASS. Any
other verdict fails the test with its reason: BLOCKED, FAIL, NOT_RUN and
NOT_APPLICABLE are never skips, and DIAGNOSTIC_OK, WITNESSED and
NOT_WITNESSED are never passes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rail  # noqa: E402

pytestmark = pytest.mark.gate_loop
HERE = Path(__file__).resolve().parent
SCENARIOS = [s for s in os.environ.get("GL_CASES", ",".join(rail.CASES)).split(",") if s]
EXPECTED = [cid for s in SCENARIOS for cid in rail.CASE_IDS[s]]


@pytest.fixture(scope="session")
def summary() -> dict:
    r = subprocess.run(
        [
            sys.executable,
            str(HERE / "rail.py"),
            "run",
            "--profile",
            "ed25519",
            "--cases",
            ",".join(SCENARIOS),
        ],
        capture_output=True,
        text=True,
        timeout=7200,
    )
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    ev = json.loads(r.stdout.strip().splitlines()[-1])["evidence"]
    return json.loads((Path(ev) / "summary.json").read_text())


@pytest.mark.parametrize("case_id", EXPECTED)
def test_case(summary: dict, case_id: str) -> None:
    got = [r for r in summary["results"] if r["id"] == case_id]
    assert got, f"{case_id}: not produced by this run"
    assert all(r["verdict"] == "PASS" for r in got), got
