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


def test_logs_keeps_every_matching_line_when_unlimited(mp: pytest.MonkeyPatch) -> None:
    import subprocess

    lines = "\n".join(f"line {i} side_effects" for i in range(60))
    mp.setattr(
        rail,
        "sh",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout=lines, stderr=""),
    )
    assert len(rail.logs("l9e2e-eie", "t0", ("side_effects",))) == 40
    full = rail.logs("l9e2e-eie", "t0", ("side_effects",), limit=None)
    assert len(full) == 60 and full[0] == "line 0 side_effects"


def test_graph_return_targets_parses_psql_rows(mp: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_psql(query: str) -> list[str]:
        seen.append(query)
        return ["retained|||2026-11-24 15:00:00+00", "consumed|converge:op-1|seeded|2026-11-24"]

    mp.setattr(rail, "psql", fake_psql)
    rows = rail.graph_return_targets("sn-fb-1")
    assert "entity_id='sn-fb-1'" in seen[0] and "tenant_id='synthneutral'" in seen[0]
    assert rows[0]["state"] == "retained" and rows[0]["consumed_by"] == ""
    assert rows[1] == {
        "state": "consumed",
        "consumed_by": "converge:op-1",
        "outcome": "seeded",
        "created_at": "2026-11-24",
    }


def test_graph_leg_outcomes_are_per_entity() -> None:
    import json

    def rec(**kw: object) -> str:
        return json.dumps({"event": "side_effects_graph_leg_resolved", **kw})

    log = [rec(entity_id="other", synced=False), rec(entity_id="gl-1", synced=True)]
    assert rail.graph_leg_outcomes(log, "gl-1") == [True]
    assert rail.graph_leg_outcomes(log, "other") == [False]
    assert rail.graph_leg_outcomes(log, "absent") == []


def test_recovery_poll_ignores_a_row_read_after_the_deadline(mp: pytest.MonkeyPatch) -> None:
    clock = iter([0.0, 0.0, 130.0, 130.0])
    mp.setattr(rail.time, "monotonic", lambda: next(clock))
    mp.setattr(rail.time, "sleep", lambda s: None)
    calls: list[int] = []

    def late_row(db: str, fid: str, timeout: float = 120) -> dict:
        calls.append(timeout)
        return {"rows": [[f'"{fid}"']], "facility_count": 1}

    mp.setattr(rail, "facility", late_row)
    g = rail.poll_recovered_facility("gl-down-1", budget_s=120)
    assert calls == [120]
    assert g["facility_count"] is None


def test_recovery_poll_accepts_a_row_within_the_deadline(mp: pytest.MonkeyPatch) -> None:
    clock = iter([0.0, 5.0, 20.0])
    mp.setattr(rail.time, "monotonic", lambda: next(clock))
    mp.setattr(rail, "facility", lambda db, fid, timeout=120: {"rows": [], "facility_count": 1})
    assert rail.poll_recovered_facility("gl-down-1", budget_s=120)["facility_count"] == 1


PLASTICOS_CHILD = {"code": "HDPE", "line_id": "l1", "direction": "in", "form": "Regrind"}
SYNTH_CHILD = {"code": "K1", "slot_id": "s1", "direction": "in"}


def _rig_converge_sync(
    mp: pytest.MonkeyPatch,
    *,
    polymers_prop: str = "NULL",
    state: str = "completed",
    enriched: object = None,
    edges: list[dict] | None = None,
) -> None:
    """Drive c_converge_sync with no Docker: a row reached CEG through Gate.

    ``enriched`` is the child list in the converge answer; ``edges`` what child_edges reads back.
    """
    mp.setattr(rail.time, "sleep", lambda s: None)
    mp.setattr(rail, "stamp", lambda: "2026-11-24T00:00:00Z")
    mp.setattr(
        rail,
        "drive",
        lambda scenario, args=None: {
            "ok": True,
            "gate_signature_valid": True,
            "response": {"signing_key_id": "gate-gl", "generation": 2, "causation_id": "c-1"},
            "payload": {
                "state": state,
                "fields": {"processed_polymers": enriched, "handled_kinds": enriched},
            },
        },
    )
    mp.setattr(rail, "psql", lambda q: ["1"])
    mp.setattr(rail, "child_edges", lambda tenant, eid: list(edges or []))
    # One Facility row, no PROCESSES edge; column 7 is the Facility's processed_polymers property.
    row = ['"fid"', "x", "x", "NULL", "NULL", "NULL", "NULL", polymers_prop]
    mp.setattr(rail, "facility", lambda db, fid, timeout=120: {"rows": [row], "facility_count": 1})

    def station(db: str, q: str, timeout: int = 120) -> list[list[str]]:
        sid = q.split("station_id:'")[1].split("'")[0]
        return [[f'"{sid}"', "NULL"]]

    mp.setattr(rail, "cypher", station)
    mp.setattr(
        rail,
        "gate_events",
        lambda since, action: [
            {"event": "gate.ingress", "source_node": "enrichment-engine", "request_id": "r1"},
            {
                "event": "gate.completed",
                "source_node": "graph",
                "request_id": "r1",
                "generation": 2,
            },
        ],
    )
    mp.setattr(rail, "logs", lambda *a, **k: [])
    mp.setattr(rail, "graph_leg_synced", lambda log, eid: True)
    mp.setattr(rail, "graph_leg_outcomes", lambda log, eid: [True])
    mp.setattr(rail, "verdict", lambda ok, p, signed_claim: "PASS" if ok else "FAIL")


def _by_id(results: list[rail.Result]) -> dict[str, rail.Result]:
    return {r.id: r for r in results}


def test_p18_passes_when_every_enriched_child_is_a_typed_edge(mp: pytest.MonkeyPatch) -> None:
    edge = {"tenant": "plasticos", **PLASTICOS_CHILD}
    _rig_converge_sync(mp, enriched=[PLASTICOS_CHILD], edges=[edge])
    p18 = _by_id(rail.c_converge_sync("ed25519"))["P18"]
    assert p18.verdict == "PASS"
    assert p18.reason == rail.GATE_BINDING_LIMIT


def test_p18_fails_when_an_enriched_child_has_no_typed_edge(mp: pytest.MonkeyPatch) -> None:
    _rig_converge_sync(mp, enriched=[PLASTICOS_CHILD], edges=[])
    p18 = _by_id(rail.c_converge_sync("ed25519"))["P18"]
    assert p18.verdict == "FAIL"
    assert "no typed edge" in p18.reason


def test_p18_fails_when_the_edge_loses_line_identity_or_form(mp: pytest.MonkeyPatch) -> None:
    edge = {"tenant": "plasticos", **PLASTICOS_CHILD, "form": None}
    _rig_converge_sync(mp, enriched=[PLASTICOS_CHILD], edges=[edge])
    assert _by_id(rail.c_converge_sync("ed25519"))["P18"].verdict == "FAIL"


def test_p18_fails_on_a_stringified_child_list(mp: pytest.MonkeyPatch) -> None:
    edge = {"tenant": "plasticos", **PLASTICOS_CHILD}
    _rig_converge_sync(mp, polymers_prop='"STRING"', enriched=[PLASTICOS_CHILD], edges=[edge])
    p18 = _by_id(rail.c_converge_sync("ed25519"))["P18"]
    assert p18.verdict == "FAIL"
    assert "stringified" in p18.reason


def test_p18_fails_when_the_provider_supplied_no_child_list(mp: pytest.MonkeyPatch) -> None:
    _rig_converge_sync(mp, enriched=None, edges=[])
    p18 = _by_id(rail.c_converge_sync("ed25519"))["P18"]
    assert p18.verdict == "FAIL"
    assert "no enriched processed_polymers list" in p18.reason


def test_p05_passes_with_typed_handles_edges_and_fails_under_another_tenant(
    mp: pytest.MonkeyPatch,
) -> None:
    _rig_converge_sync(
        mp, enriched=[SYNTH_CHILD], edges=[{"tenant": "synthneutral", **SYNTH_CHILD}]
    )
    assert _by_id(rail.c_converge_sync("ed25519", tenant="synthneutral"))["P05"].verdict == "PASS"
    _rig_converge_sync(mp, enriched=[SYNTH_CHILD], edges=[{"tenant": "plasticos", **SYNTH_CHILD}])
    p05 = _by_id(rail.c_converge_sync("ed25519", tenant="synthneutral"))["P05"]
    assert p05.verdict == "FAIL"
    assert "another tenant" in p05.reason


def test_p05_incomplete_converge_fails(mp: pytest.MonkeyPatch) -> None:
    edge = {"tenant": "synthneutral", **SYNTH_CHILD}
    _rig_converge_sync(mp, state="failed", enriched=[SYNTH_CHILD], edges=[edge])
    p05 = _by_id(rail.c_converge_sync("ed25519", tenant="synthneutral"))["P05"]
    assert p05.verdict == "FAIL"
    assert "converge state 'failed'" in p05.reason


def test_p07_reason_records_the_gate_entity_binding_limit(mp: pytest.MonkeyPatch) -> None:
    _rig_converge_sync(mp)
    p07 = _by_id(rail.c_converge_sync("ed25519"))["P07"]
    assert p07.verdict == "PASS"
    assert rail.GATE_BINDING_LIMIT in p07.reason


def test_enriched_request_omits_the_child_list_and_names_it_in_the_schema() -> None:
    for tenant in ("plasticos", "synthneutral"):
        body = rail.converge_payload("e1", tenant, enrich_children=True)["payload"]
        child = rail.CHILD_FIELD[tenant]
        assert child not in body["entity"]
        assert body["schema"][child] == rail.ENRICHED_CHILD_TYPE
        plain = rail.converge_payload("e1", tenant)["payload"]
        assert child in plain["entity"] and child not in plain["schema"]


def test_recovery_poll_gives_the_probe_the_fractional_remaining_budget(
    mp: pytest.MonkeyPatch,
) -> None:
    clock = iter([0.0, 118.1, 119.0])
    mp.setattr(rail.time, "monotonic", lambda: next(clock))
    calls: list[float] = []

    def row(db: str, fid: str, timeout: float = 120) -> dict:
        calls.append(timeout)
        return {"rows": [], "facility_count": 1}

    mp.setattr(rail, "facility", row)
    assert rail.poll_recovered_facility("gl-down-1", budget_s=120)["facility_count"] == 1
    assert calls == [pytest.approx(1.9)]


def test_recovery_poll_never_gives_a_probe_more_than_a_sub_second_budget(
    mp: pytest.MonkeyPatch,
) -> None:
    clock = iter([0.0, 119.6, 120.0])
    mp.setattr(rail.time, "monotonic", lambda: next(clock))
    calls: list[float] = []

    def timed_out(db: str, fid: str, timeout: float = 120) -> dict:
        calls.append(timeout)
        raise rail.ProbeTimeoutError("cypher on plasticos failed (rc 124)")

    mp.setattr(rail, "facility", timed_out)
    g = rail.poll_recovered_facility("gl-down-1", budget_s=120)
    assert calls == [pytest.approx(0.4)]
    assert g["facility_count"] is None


def test_recovery_poll_raises_a_non_timeout_error_at_the_deadline(mp: pytest.MonkeyPatch) -> None:
    clock = iter([0.0, 119.6, 120.0])
    mp.setattr(rail.time, "monotonic", lambda: next(clock))

    def broken(db: str, fid: str, timeout: float = 120) -> dict:
        raise rail.ProbeError("cypher on plasticos failed (rc 1): auth failure")

    mp.setattr(rail, "facility", broken)
    with pytest.raises(rail.ProbeError, match="auth failure"):
        rail.poll_recovered_facility("gl-down-1", budget_s=120)


def test_cypher_classifies_only_the_sh_timeout_as_a_probe_timeout(mp: pytest.MonkeyPatch) -> None:
    mp.setattr(rail, "env", lambda: {"GL_NEO4J_SECRET": "x"})
    mp.setattr(
        rail,
        "sh",
        lambda cmd, timeout=600, extra_env=None: rail.subprocess.CompletedProcess(
            cmd[:1], 124, "", "docker: timed out after 0.4s"
        ),
    )
    with pytest.raises(rail.ProbeTimeoutError):
        rail.cypher("plasticos", "RETURN 1", timeout=0.4)
    mp.setattr(
        rail,
        "sh",
        lambda cmd, timeout=600, extra_env=None: rail.subprocess.CompletedProcess(
            cmd[:1], 1, "", "auth failure"
        ),
    )
    with pytest.raises(rail.ProbeError) as err:
        rail.cypher("plasticos", "RETURN 1")
    assert not isinstance(err.value, rail.ProbeTimeoutError)


def test_uncorrelated_egress_signing_error_never_turns_a_failure_into_blocked(
    mp: pytest.MonkeyPatch,
) -> None:
    """Another entity's signing error in the window must not relabel this entity's FAIL."""
    _rig_converge_sync(mp)
    mp.setattr(rail, "facility", lambda db, fid, timeout=120: {"rows": [], "facility_count": 0})
    mp.setattr(rail, "graph_leg_outcomes", lambda log, eid: [False])
    mp.setattr(
        rail,
        "logs",
        lambda *a, **k: [
            '{"event": "packet_route_error", "detail": "TransportAuthenticationError: '
            'signature required but not present"}'
        ],
    )
    results = _by_id(rail.c_converge_sync("ed25519"))
    for case in ("P07", "P18"):
        assert results[case].verdict == "FAIL"
        assert rail.EGRESS_SIGNING_UNCORRELATED in results[case].reason


def test_p27_requires_the_consuming_converge_to_have_completed() -> None:
    ok = {
        "accepted": True,
        "converge1_completed": True,
        "consumed": True,
        "retained": True,
        "consumed_row": True,
    }
    assert all(rail.p27_requirements(**ok).values())
    failed = rail.p27_requirements(**{**ok, "converge1_completed": False})
    assert not all(failed.values())
    assert [k for k, v in failed.items() if not v] == ["converge completed"]


def test_p27_counts_only_a_target_the_first_converge_consumed() -> None:
    consumed = {"state": "consumed", "consumed_by": "converge:op-1", "outcome": "seeded"}
    retained = {"state": "retained", "consumed_by": "", "outcome": ""}
    assert rail.c1_consumed_rows([consumed]) == [consumed]
    # c1 left it retained; whatever the second converge did is not c1's evidence.
    assert rail.c1_consumed_rows([retained]) == []
    assert rail.c1_consumed_rows([{**consumed, "outcome": "superseded"}]) == []
    assert rail.c1_consumed_rows([{**consumed, "consumed_by": "sweep:1"}]) == []
