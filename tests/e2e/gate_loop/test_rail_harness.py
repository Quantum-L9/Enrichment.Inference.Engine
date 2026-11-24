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


def test_sdk_pin_requires_every_process_at_the_expected_release() -> None:
    exp = rail.EXPECTED_SDK
    other = "1.3.2 50 ecd911d5ab28d6d7b246e6b93ab48a2ad0fb2293f1f53c26a900bb42a995f929"
    same = dict.fromkeys(("l9e2e-gate", "l9e2e-eie", "l9e2e-ceg"), exp)
    assert rail.sdk_pin(same, exp)["match"] is True
    one_old = {**same, "l9e2e-ceg": other}
    pin = rail.sdk_pin(one_old, exp)
    assert pin["match"] is False
    assert pin["mismatched"] == {"l9e2e-ceg": other}
    unavailable = {**same, "l9e2e-eie": "UNAVAILABLE rc=1"}
    assert rail.sdk_pin(unavailable, exp)["match"] is False
    assert rail.sdk_pin({}, exp)["match"] is False
    with pytest.raises(ValueError, match="expected SDK line"):
        rail.sdk_pin(same, "")


def test_sdk_pin_mismatch_runs_no_case_and_writes_each_result(
    tmp_path: Path, mp: pytest.MonkeyPatch
) -> None:
    import json

    other = "1.3.2 50 ecd911d5ab28d6d7b246e6b93ab48a2ad0fb2293f1f53c26a900bb42a995f929"
    ran: list[str] = []

    def must_not_run(p: str) -> list[rail.Result]:
        ran.append("case")
        return [rail.Result("T1", "A8", p, "case", "PASS")]

    mp.setattr(rail, "CASES", {"one": must_not_run, "two": must_not_run})
    mp.setattr(rail, "CASE_IDS", {"one": ("T1",), "two": ("T2", "T3")})
    mp.setattr(rail, "STATE", tmp_path)
    mp.setattr(rail, "running_signing", lambda: {})
    mp.setattr(rail, "wait_ready", lambda: {"ready": True})
    mp.setattr(rail, "secrets_", lambda: [])
    mp.setattr(
        rail,
        "release_set",
        lambda p: {
            "sdk_code_digest": dict.fromkeys(("l9e2e-gate", "l9e2e-eie", "l9e2e-ceg"), other)
        },
    )
    mp.delenv("GL_EXPECT_SDK", raising=False)
    mp.setattr(sys, "argv", ["rail.py", "run", "--no-boot", "--cases", "one,two"])

    assert rail.main() == 5
    assert ran == []
    (ev,) = (tmp_path / "evidence").iterdir()
    for cid in ("T1", "T2", "T3"):
        res = json.loads((ev / f"{cid}.json").read_text())
        assert res["verdict"] == "NOT_RUN"
        assert res["reason"].startswith("SDK pin mismatch")
    summary = json.loads((ev / "summary.json").read_text())
    assert summary["aborted"].startswith("SDK pin mismatch")


@pytest.mark.parametrize("probe_fails", [False, True])
@pytest.mark.parametrize("restored", ["sha256:verified", "sha256:rebuilt"])
def test_rogue_signer_continues_only_on_the_verified_graph_image(
    mp: pytest.MonkeyPatch, restored: str, probe_fails: bool
) -> None:
    import subprocess

    image = restored + "\n"

    exp = rail.EXPECTED_SDK
    mp.setattr(
        rail,
        "RELEASE_SET",
        {"sdk_pin": {"expected": exp}, "images": {"l9e2e-ceg": {"image_id": "sha256:verified"}}},
    )
    mp.setattr(
        rail, "rail", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr="")
    )
    mp.setattr(rail.time, "sleep", lambda s: None)
    mp.setattr(rail, "stamp", lambda: "t0")

    def drive(*a: object, **k: object) -> dict:
        if probe_fails:
            raise rail.ProbeError("driver exited 1")
        return {"http_status": 502, "request_packet_id": "pk"}

    mp.setattr(rail, "drive", drive)
    mp.setattr(rail, "gate_events_for", lambda since, pid: [])
    mp.setattr(rail, "wait_ready", lambda: {"ready": True})
    mp.setattr(
        rail, "sdk_digests", lambda: dict.fromkeys(("l9e2e-gate", "l9e2e-eie", "l9e2e-ceg"), exp)
    )
    mp.setattr(
        rail,
        "sh",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout=image, stderr=""),
    )
    if restored == "sha256:verified" and probe_fails:
        # Verified restore: the probe's own error propagates (NOT_RUN), later cases may continue.
        with pytest.raises(rail.ProbeError) as err:
            rail.c_rogue_signer("ed25519")
        assert not isinstance(err.value, rail.StackUnusableError)
    elif restored == "sha256:verified":
        (res,) = rail.c_rogue_signer("ed25519")
        assert res.id == "P12"
    else:
        # A rebuilt restore stops the run whether or not the probe failed.
        with pytest.raises(rail.StackUnusableError, match="not the verified image"):
            rail.c_rogue_signer("ed25519")


@pytest.mark.parametrize("probe_fails", [False, True])
def test_unverifiable_graph_restore_stops_later_cases(
    tmp_path: Path, mp: pytest.MonkeyPatch, probe_fails: bool
) -> None:
    import subprocess

    mp.setattr(
        rail,
        "RELEASE_SET",
        {
            "sdk_pin": {"expected": rail.EXPECTED_SDK},
            "images": {"l9e2e-ceg": {"image_id": "sha256:verified"}},
        },
    )
    mp.setattr(
        rail, "rail", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr="")
    )
    mp.setattr(rail.time, "sleep", lambda s: None)
    mp.setattr(rail, "stamp", lambda: "t0")
    mp.setattr(rail, "gate_events_for", lambda since, pid: [])
    mp.setattr(rail, "wait_ready", lambda: {"ready": True})

    def drive(*a: object, **k: object) -> dict:
        if probe_fails:
            raise rail.ProbeError("driver exited 1")
        return {"http_status": 502, "request_packet_id": "pk"}

    def docker_unavailable() -> dict:
        raise OSError("docker: not found")

    mp.setattr(rail, "drive", drive)
    mp.setattr(rail, "sdk_digests", docker_unavailable)
    ran: list[str] = []

    def later(p: str) -> list[rail.Result]:
        ran.append("later")
        return [rail.Result("T2", "A8", p, "later", "PASS")]

    mp.setattr(rail, "CASES", {"rogue": rail.c_rogue_signer, "later": later})
    mp.setattr(rail, "CASE_IDS", {"rogue": ("P12",), "later": ("T2",)})
    mp.setattr(rail, "ENV_FILE", tmp_path / "absent.env")

    results, aborted = rail.run_cases(["rogue", "later"], "ed25519", tmp_path)

    assert ran == []
    assert aborted is not None and "could not be verified" in aborted
    assert [(r.id, r.verdict) for r in results] == [("P12", "NOT_RUN"), ("T2", "NOT_RUN")]


def test_mixed_alg_outcome_classifies_the_worker_side() -> None:
    accepted = [
        {
            "event": "gate.failure",
            "error_type": "GateDispatchSecurityError",
            "error_message": "worker response failed transport validation: unknown key",
        }
    ]
    refused = [
        {
            "event": "gate.failure",
            "error_type": "WorkerHTTPError",
            "error_message": "worker 'graph' execute returned HTTP 401",
        }
    ]
    assert rail.mixed_alg_outcome(502, accepted) == "accepted"
    assert rail.mixed_alg_outcome(502, refused) == "refused"
    assert rail.mixed_alg_outcome(502, []) == "unproven"
    assert rail.mixed_alg_outcome(200, accepted) == "unproven"


@pytest.mark.parametrize(
    ("error_type", "message", "expected"),
    [
        ("GateDispatchSecurityError", "worker response failed transport validation", "PASS"),
        ("WorkerHTTPError", "worker 'graph' execute returned HTTP 403", "FAIL"),
    ],
)
def test_mixed_alg_case_needs_the_window_and_restores_the_worker(
    mp: pytest.MonkeyPatch, error_type: str, message: str, expected: str
) -> None:
    import subprocess

    calls: list[dict] = []
    mp.setattr(
        rail,
        "rail",
        lambda *a, **k: calls.append(k) or subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
    )
    mp.setattr(rail.time, "sleep", lambda s: None)
    mp.setattr(rail, "stamp", lambda: "t0")
    mp.setattr(rail, "drive", lambda *a, **k: {"http_status": 502, "request_packet_id": "pk"})
    mp.setattr(
        rail,
        "gate_events_for",
        lambda since, pid: [
            {"event": "gate.failure", "error_type": error_type, "error_message": message}
        ],
    )
    signing = iter(
        [
            {"l9e2e-gate": "ed25519", "l9e2e-eie": "ed25519", "l9e2e-ceg": "hmac-sha256"},
            dict.fromkeys(("l9e2e-gate", "l9e2e-eie", "l9e2e-ceg"), "ed25519"),
        ]
    )
    mp.setattr(rail, "running_signing", lambda: next(signing))
    mp.setattr(
        rail, "OBSERVED_PROFILE", dict.fromkeys(("l9e2e-gate", "l9e2e-eie", "l9e2e-ceg"), "ed25519")
    )
    mp.setattr(rail, "_restore_trusted_graph", lambda: ({"ready": True}, {}))

    (res,) = rail.c_mixed_alg("ed25519")

    assert calls[0]["extra_env"] == {"GL_MIXED_CEG": "1"}
    assert res.id == "MIX-ALG-01"
    assert res.verdict == expected


def test_graph_leg_synced_binds_the_sync_to_this_entity() -> None:
    import json

    def rec(**kw: object) -> str:
        return json.dumps({"event": "side_effects_graph_leg_resolved", **kw})

    log = ["not json", rec(entity_id="other", synced=True), rec(entity_id="gl-1", synced=False)]
    assert rail.graph_leg_synced(log, "gl-1") is False
    assert rail.graph_leg_synced([*log, rec(entity_id="gl-1", synced=True)], "gl-1") is True
    assert rail.graph_leg_synced([json.dumps(["list"])], "gl-1") is False
