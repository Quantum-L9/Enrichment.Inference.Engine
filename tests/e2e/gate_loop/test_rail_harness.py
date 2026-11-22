"""Harness behaviour of rail.py that needs no Docker rail (always collected).

A case that cannot restore the stack raises StackUnusableError. The run must stop there: no later
case may run against the modified stack, the stopping case and every later case are NOT_RUN,
and main() reports the abort (non-zero exit).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rail  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def mp(monkeypatch):  # type: ignore[no-untyped-def]
    """pytest's monkeypatch under a short name."""
    return monkeypatch


def test_stack_unusable_stops_later_cases(tmp_path: Path, mp: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    def ok(p: str) -> list[rail.Result]:
        ran.append("first")
        return [rail.Result("T1", "A8", p, "first", "PASS")]

    def breaks_stack(p: str) -> list[rail.Result]:
        ran.append("restore_fails")
        raise rail.StackUnusableError("graph registration not restored; stop using this stack")

    def must_not_run(p: str) -> list[rail.Result]:
        ran.append("later")
        return [rail.Result("T3", "A8", p, "later", "PASS")]

    mp.setattr(rail, "CASES", {"first": ok, "restore_fails": breaks_stack, "later": must_not_run})
    mp.setattr(
        rail,
        "CASE_IDS",
        {"first": ("T1",), "restore_fails": ("T2a", "T2b"), "later": ("T3",)},
    )
    mp.setattr(rail, "ENV_FILE", tmp_path / "absent.env")

    results, aborted = rail.run_cases(["first", "restore_fails", "later"], "ed25519", tmp_path)

    assert ran == ["first", "restore_fails"]
    assert aborted is not None and "restore_fails" in aborted
    assert [(r.id, r.verdict) for r in results] == [
        ("T1", "PASS"),
        ("T2a", "NOT_RUN"),
        ("T2b", "NOT_RUN"),
        ("T3", "NOT_RUN"),
    ]
    assert "not run: stack unusable after case 'restore_fails'" in results[-1].reason


def test_other_probe_errors_do_not_stop_the_run(tmp_path: Path, mp: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    def probe_fails(p: str) -> list[rail.Result]:
        ran.append("probe_fails")
        raise rail.ProbeError("cypher on plasticos failed (rc 1)")

    def later(p: str) -> list[rail.Result]:
        ran.append("later")
        return [rail.Result("T2", "A8", p, "later", "PASS")]

    mp.setattr(rail, "CASES", {"probe_fails": probe_fails, "later": later})
    mp.setattr(rail, "CASE_IDS", {"probe_fails": ("T1",), "later": ("T2",)})
    mp.setattr(rail, "ENV_FILE", tmp_path / "absent.env")

    results, aborted = rail.run_cases(["probe_fails", "later"], "ed25519", tmp_path)

    assert ran == ["probe_fails", "later"]
    assert aborted is None
    assert [(r.id, r.verdict) for r in results] == [("T1", "NOT_RUN"), ("T2", "PASS")]


def test_sdk_digest_requires_exit_zero_and_a_sha256(mp: pytest.MonkeyPatch) -> None:
    import subprocess

    good = "1.3.2 41 " + "ab" * 32
    replies = {
        "l9e2e-gate": (0, good),
        "l9e2e-eie": (1, good),  # right output, failed exit: not a digest
        "l9e2e-ceg": (0, "Traceback (most recent call last)"),  # exit 0, not a digest line
    }

    def fake_sh(cmd: list[str], **_: object) -> subprocess.CompletedProcess:
        rc, out = replies[cmd[2]]
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")

    mp.setattr(rail, "sh", fake_sh)
    got = rail.sdk_digests()
    assert got["l9e2e-gate"] == good
    assert got["l9e2e-eie"] == "UNAVAILABLE rc=1"
    assert got["l9e2e-ceg"] == "UNAVAILABLE rc=0"
