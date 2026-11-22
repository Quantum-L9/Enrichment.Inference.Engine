"""Opt-in pytest entry for the gate_loop rail (see README.md).

Runs rail.py once per session against an already-built image set, then turns
each recorded case into its own test. A case passes only when the rail
recorded PASS. Any other verdict fails the test and states its reason:
BLOCKED, FAIL, NOT_RUN and NOT_APPLICABLE are never skips, and
DIAGNOSTIC_OK is never a pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.gate_loop
HERE = Path(__file__).resolve().parent
CASES = os.environ.get(
    "GL_CASES",
    "isolation,policy,negatives,converge_plasticos,converge_synthneutral,"
    "sync_rejects,match,inference_dup,ceg_down,rogue_signer,gate_restart",
)


@pytest.fixture(scope="session")
def summary() -> dict:
    r = subprocess.run(
        [sys.executable, str(HERE / "rail.py"), "run", "--profile", "ed25519", "--cases", CASES],
        capture_output=True,
        text=True,
        timeout=7200,
    )
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    ev = json.loads(r.stdout.strip().splitlines()[-1])["evidence"]
    return json.loads((Path(ev) / "summary.json").read_text())


def _ids() -> list[str]:
    return [
        "P03",
        "P05",
        "P07",
        "P09",
        "P10",
        "P11",
        "P12",
        "P13",
        "P14",
        "P17",
        "P18",
        "P19",
        "P23",
        "P33",
        "P34",
    ]


@pytest.mark.parametrize("case_id", _ids())
def test_case(summary: dict, case_id: str) -> None:
    got = [r for r in summary["results"] if r["id"] == case_id]
    assert got, f"{case_id}: not produced by this run"
    assert all(r["verdict"] == "PASS" for r in got), got
