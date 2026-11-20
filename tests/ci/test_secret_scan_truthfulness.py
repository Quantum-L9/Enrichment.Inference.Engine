"""Secret-scan jobs state plainly when they did not scan (campaign findings T05, T06).

A job that did not execute its scanner must not end green
(l9.contract/validation-and-correctness@1: no
`default_success_on_missing_unreadable_unexecuted_or_unresolved_required_validation`,
no `silent_skip_of_applicable_required_validation`).

T05, `.github/workflows/gitleaks.yml`: Dependabot events cannot read
`GITLEAKS_LICENSE`, so `gitleaks-action` is skipped for that actor. The
licence-free Gitleaks binary scans those events instead, before merge.
T06, `.github/workflows/gitguardian.yml`: a missing key, a key GitGuardian
rejects and an unreachable API each report `NOT_EXECUTED` and fail the job.

Each witness extracts the step's `run` script from the workflow and executes
it under the shell GitHub uses for `shell: bash`
(`bash --noprofile --norc -eo pipefail {0}`; workflow syntax reference,
`jobs.<job_id>.steps[*].shell`), with a stub for the network or scanner binary.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

GITLEAKS = "gitleaks.yml"
GITGUARDIAN = "gitguardian.yml"
DEPENDABOT = "github.actor == 'dependabot[bot]'"
NOT_DEPENDABOT = "github.actor != 'dependabot[bot]'"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")


def _job_steps(workflow: str, job: str) -> list[dict]:
    data = yaml.safe_load((WORKFLOWS / workflow).read_text())
    return data["jobs"][job]["steps"]


def _step(workflow: str, job: str, name: str) -> dict:
    for step in _job_steps(workflow, job):
        if step.get("name") == name:
            return step
    raise AssertionError(f"{workflow}: no step {name!r}")


def _stub(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / name
    stub.write_text(f"#!/bin/sh\n{body}\n")
    stub.chmod(0o755)


class StepRun:
    def __init__(self, returncode: int, output: str, summary: str, log: str) -> None:
        self.returncode = returncode
        self.output = output
        self.summary = summary
        self.log = log


def _run_step(step: dict, tmp_path: Path, env: dict[str, str]) -> StepRun:
    """Run a workflow step's script the way the runner does for `shell: bash`."""
    assert step.get("shell") == "bash", f"{step.get('name')}: witness needs `shell: bash`"
    assert "${{" not in step["run"], f"{step.get('name')}: expressions belong in `env:`"
    script = tmp_path / "step.sh"
    script.write_text(step["run"])
    output = tmp_path / "github_output"
    summary = tmp_path / "summary"
    github_path = tmp_path / "github_path"
    for file in (output, summary, github_path):
        file.write_text("")
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir(exist_ok=True)
    full_env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
        "GITHUB_PATH": str(github_path),
        "RUNNER_TEMP": str(runner_temp),
        **env,
    }
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=tmp_path,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )
    return StepRun(
        proc.returncode, output.read_text(), summary.read_text(), proc.stdout + proc.stderr
    )


# ── T06: GitGuardian ──────────────────────────────────────────────────


def _preflight(tmp_path: Path, *, key: str, curl_body: str) -> StepRun:
    _stub(tmp_path / "bin", "curl", curl_body)
    step = _step(GITGUARDIAN, "scan", "Verify GitGuardian can scan")
    env = {name: value for name, value in step["env"].items() if "${{" not in str(value)}
    return _run_step(step, tmp_path, {**env, "GITGUARDIAN_API_KEY": key})


def test_gitguardian_missing_key_is_not_executed_and_fails(tmp_path: Path) -> None:
    """Witness: no key -> NOT_EXECUTED (token_missing), red, and the API is never called."""
    marker = tmp_path / "curl_called"
    run = _preflight(tmp_path, key="", curl_body=f"touch {marker}; printf 200")
    assert run.returncode != 0
    assert "status=NOT_EXECUTED" in run.output
    assert "reason=token_missing" in run.output
    assert "status=READY" not in run.output
    assert not marker.exists()


@pytest.mark.parametrize("http_code", ["401", "403"])
def test_gitguardian_rejected_key_is_not_executed_and_fails(tmp_path: Path, http_code: str) -> None:
    """Witness: an invalid or expired key -> NOT_EXECUTED (token_rejected), red."""
    run = _preflight(tmp_path, key="stub-key", curl_body=f"printf {http_code}")
    assert run.returncode != 0
    assert "status=NOT_EXECUTED" in run.output
    assert "reason=token_rejected" in run.output
    assert "status=READY" not in run.output


@pytest.mark.parametrize(
    "curl_body",
    ["printf 500", "printf 503", "printf 000; exit 6", "printf 000; exit 28"],
    ids=["http-500", "http-503", "dns-failure", "timeout"],
)
def test_gitguardian_api_failure_is_not_executed_and_fails(tmp_path: Path, curl_body: str) -> None:
    """Witness: the API errors or cannot be reached -> NOT_EXECUTED (api_unavailable), red."""
    run = _preflight(tmp_path, key="stub-key", curl_body=curl_body)
    assert run.returncode != 0
    assert "status=NOT_EXECUTED" in run.output
    assert "reason=api_unavailable" in run.output
    assert "status=READY" not in run.output


def test_gitguardian_accepted_key_is_ready(tmp_path: Path) -> None:
    """Control: the successful path is intact — a valid key lets the scan proceed."""
    run = _preflight(tmp_path, key="stub-key", curl_body="printf 200")
    assert run.returncode == 0
    assert "status=READY" in run.output
    assert "NOT_EXECUTED" not in run.output


def test_gitguardian_preflight_never_prints_the_key(tmp_path: Path) -> None:
    run = _preflight(tmp_path, key="stub-key-value-7f3a", curl_body="printf 401")
    assert "stub-key-value-7f3a" not in run.log
    assert "stub-key-value-7f3a" not in run.output


def test_gitguardian_scan_steps_have_no_skip_or_soft_fail_path() -> None:
    """After the preflight nothing can skip the scan or swallow its failure."""
    steps = _job_steps(GITGUARDIAN, "scan")
    names = [step.get("name") for step in steps]
    assert names.index("Verify GitGuardian can scan") < names.index("GitGuardian scan")
    scan = _step(GITGUARDIAN, "scan", "GitGuardian scan")
    assert scan["uses"].startswith("GitGuardian/ggshield-action@")
    assert "if" not in scan
    for step in steps:
        assert not step.get("continue-on-error"), step.get("name")
    data = yaml.safe_load((WORKFLOWS / GITGUARDIAN).read_text())
    assert "if" not in data["jobs"]["scan"]
    assert not data["jobs"]["scan"].get("continue-on-error")


def _gitguardian_report(tmp_path: Path, **env: str) -> StepRun:
    step = _step(GITGUARDIAN, "scan", "Report scan status")
    assert step["if"] == "always()"
    base = {"ACTOR": "octocat", "EVENT_NAME": "pull_request", "HEAD_SHA": "0" * 40}
    return _run_step(step, tmp_path, {**base, **env})


def test_gitguardian_report_clean_scan(tmp_path: Path) -> None:
    run = _gitguardian_report(tmp_path, PREFLIGHT_STATUS="READY", SCAN_OUTCOME="success")
    assert run.returncode == 0
    assert "`EXECUTED_CLEAN`" in run.summary


def test_gitguardian_report_failed_scan_is_not_a_pass(tmp_path: Path) -> None:
    run = _gitguardian_report(tmp_path, PREFLIGHT_STATUS="READY", SCAN_OUTCOME="failure")
    assert "`FAILED`" in run.summary
    assert "EXECUTED_CLEAN" not in run.summary


@pytest.mark.parametrize(
    "env",
    [
        {"PREFLIGHT_STATUS": "NOT_EXECUTED", "PREFLIGHT_REASON": "token_missing"},
        {"PREFLIGHT_STATUS": "NOT_EXECUTED", "PREFLIGHT_REASON": "token_rejected"},
        {"PREFLIGHT_STATUS": "NOT_EXECUTED", "PREFLIGHT_REASON": "api_unavailable"},
        {"PREFLIGHT_STATUS": "", "PREFLIGHT_REASON": "", "SCAN_OUTCOME": "skipped"},
        {"PREFLIGHT_STATUS": "READY", "SCAN_OUTCOME": "skipped"},
        {"PREFLIGHT_STATUS": "READY", "SCAN_OUTCOME": "cancelled"},
        {"PREFLIGHT_STATUS": "READY", "SCAN_OUTCOME": ""},
    ],
)
def test_gitguardian_report_never_ends_green_without_a_scan(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = _gitguardian_report(tmp_path, **{"SCAN_OUTCOME": "skipped", **env})
    assert run.returncode != 0
    assert "`NOT_EXECUTED`" in run.summary
    assert "EXECUTED_CLEAN" not in run.summary


# ── T05: Gitleaks ─────────────────────────────────────────────────────


def test_gitleaks_every_actor_has_exactly_one_scan_path() -> None:
    """Dependabot gets the binary scan; every other actor gets gitleaks-action."""
    install = _step(GITLEAKS, "gitleaks", "Install Gitleaks binary (Dependabot events)")
    binary = _step(GITLEAKS, "gitleaks", "Gitleaks scan (Dependabot events, binary)")
    action = _step(GITLEAKS, "gitleaks", "Gitleaks scan")
    assert install["if"] == DEPENDABOT
    assert binary["if"] == DEPENDABOT
    assert action["if"] == NOT_DEPENDABOT
    assert action["uses"].startswith("gitleaks/gitleaks-action@")
    for step in _job_steps(GITLEAKS, "gitleaks"):
        assert not step.get("continue-on-error"), step.get("name")
    data = yaml.safe_load((WORKFLOWS / GITLEAKS).read_text())
    assert "if" not in data["jobs"]["gitleaks"]


def test_gitleaks_dependabot_path_reads_no_secret() -> None:
    """The Dependabot scan needs no licence and no secret (finding T05)."""
    raw = (WORKFLOWS / GITLEAKS).read_text()
    for name in (
        "Install Gitleaks binary (Dependabot events)",
        "Gitleaks scan (Dependabot events, binary)",
    ):
        step = _step(GITLEAKS, "gitleaks", name)
        assert "secrets." not in yaml.safe_dump(step)
    job_env = yaml.safe_load(raw)["jobs"]["gitleaks"]["env"]
    assert "secrets." not in yaml.safe_dump(job_env)


def _binary_scan(tmp_path: Path, gitleaks_body: str) -> StepRun:
    _stub(tmp_path / "bin", "gitleaks", gitleaks_body)
    step = _step(GITLEAKS, "gitleaks", "Gitleaks scan (Dependabot events, binary)")
    return _run_step(step, tmp_path, {})


def test_gitleaks_binary_clean_scan_is_executed_clean(tmp_path: Path) -> None:
    run = _binary_scan(tmp_path, "exit 0")
    assert run.returncode == 0
    assert "result=EXECUTED_CLEAN" in run.output


def test_gitleaks_binary_findings_fail_the_step(tmp_path: Path) -> None:
    """Witness: leaks on a Dependabot event block the job before merge."""
    run = _binary_scan(tmp_path, "exit 1")
    assert run.returncode == 1
    assert "result=EXECUTED_FINDINGS" in run.output


@pytest.mark.parametrize("exit_code", [2, 126, 127])
def test_gitleaks_binary_error_is_not_executed(tmp_path: Path, exit_code: int) -> None:
    """Witness: a scanner error is NOT_EXECUTED, never a clean result."""
    run = _binary_scan(tmp_path, f"exit {exit_code}")
    assert run.returncode == exit_code
    assert "result=NOT_EXECUTED" in run.output
    assert "EXECUTED_CLEAN" not in run.output


def test_gitleaks_binary_scan_uses_the_repository_rules(tmp_path: Path) -> None:
    args = tmp_path / "args"
    _binary_scan(tmp_path, f'echo "$@" > {args}; exit 0')
    recorded = args.read_text()
    assert "--config .gitleaks.toml" in recorded
    assert "--exit-code 1" in recorded
    assert "--redact" in recorded


def _job_env() -> dict[str, str]:
    data = yaml.safe_load((WORKFLOWS / GITLEAKS).read_text())
    return {name: str(value) for name, value in data["jobs"]["gitleaks"]["env"].items()}


def test_gitleaks_install_rejects_a_checksum_mismatch(tmp_path: Path) -> None:
    """Witness: a tampered or truncated download installs nothing and fails."""
    # The stub writes bytes whose SHA-256 is not the pinned one.
    _stub(
        tmp_path / "bin",
        "curl",
        'while [ "$#" -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done; echo tampered > "$out"',
    )
    step = _step(GITLEAKS, "gitleaks", "Install Gitleaks binary (Dependabot events)")
    run = _run_step(step, tmp_path, _job_env())
    assert run.returncode != 0
    assert "NOT_EXECUTED" in run.log
    assert "checksum mismatch" in run.log
    assert (tmp_path / "github_path").read_text() == ""


def test_gitleaks_install_reports_a_failed_download(tmp_path: Path) -> None:
    _stub(tmp_path / "bin", "curl", "exit 22")
    step = _step(GITLEAKS, "gitleaks", "Install Gitleaks binary (Dependabot events)")
    run = _run_step(step, tmp_path, _job_env())
    assert run.returncode != 0
    assert "NOT_EXECUTED" in run.log
    assert (tmp_path / "github_path").read_text() == ""


def test_gitleaks_binary_pin_is_a_version_and_a_sha256() -> None:
    env = _job_env()
    assert env["GITLEAKS_VERSION"].count(".") == 2
    assert len(env["GITLEAKS_SHA256"]) == 64
    int(env["GITLEAKS_SHA256"], 16)


def _gitleaks_report(tmp_path: Path, **env: str) -> StepRun:
    step = _step(GITLEAKS, "gitleaks", "Report scan status")
    assert step["if"] == "always()"
    base = {
        "EVENT_NAME": "pull_request",
        "HEAD_SHA": "0" * 40,
        "INSTALL_OUTCOME": "",
        "BINARY_OUTCOME": "",
        "BINARY_RESULT": "",
        "ACTION_OUTCOME": "",
        **_job_env(),
    }
    return _run_step(step, tmp_path, {**base, **env})


def test_gitleaks_report_dependabot_names_the_policy_skip_and_the_scan(tmp_path: Path) -> None:
    run = _gitleaks_report(
        tmp_path,
        ACTOR="dependabot[bot]",
        INSTALL_OUTCOME="success",
        BINARY_OUTCOME="success",
        BINARY_RESULT="EXECUTED_CLEAN",
        ACTION_OUTCOME="skipped",
    )
    assert run.returncode == 0
    assert "`EXECUTED_CLEAN`" in run.summary
    assert "SKIPPED_BY_POLICY" in run.summary
    assert "GITLEAKS_LICENSE" in run.summary


@pytest.mark.parametrize(
    "env",
    [
        {"INSTALL_OUTCOME": "failure", "BINARY_OUTCOME": "skipped"},
        {"INSTALL_OUTCOME": "skipped", "BINARY_OUTCOME": "skipped"},
        {"INSTALL_OUTCOME": "success", "BINARY_OUTCOME": "skipped"},
        {"INSTALL_OUTCOME": "success", "BINARY_OUTCOME": "failure", "BINARY_RESULT": ""},
        {
            "INSTALL_OUTCOME": "success",
            "BINARY_OUTCOME": "failure",
            "BINARY_RESULT": "NOT_EXECUTED",
        },
    ],
)
def test_gitleaks_report_dependabot_never_ends_green_without_a_scan(
    tmp_path: Path, env: dict[str, str]
) -> None:
    run = _gitleaks_report(tmp_path, ACTOR="dependabot[bot]", ACTION_OUTCOME="skipped", **env)
    assert run.returncode != 0
    assert "`NOT_EXECUTED`" in run.summary
    assert "EXECUTED_CLEAN" not in run.summary


def test_gitleaks_report_action_path(tmp_path: Path) -> None:
    clean = _gitleaks_report(tmp_path, ACTOR="octocat", ACTION_OUTCOME="success")
    assert clean.returncode == 0
    assert "`EXECUTED_CLEAN`" in clean.summary
    assert "SKIPPED_BY_POLICY" not in clean.summary


def test_gitleaks_report_action_failure_is_not_a_pass(tmp_path: Path) -> None:
    run = _gitleaks_report(tmp_path, ACTOR="octocat", ACTION_OUTCOME="failure")
    assert "`FAILED`" in run.summary
    assert "EXECUTED_CLEAN" not in run.summary


@pytest.mark.parametrize("outcome", ["skipped", "cancelled", ""])
def test_gitleaks_report_action_not_run_fails(tmp_path: Path, outcome: str) -> None:
    run = _gitleaks_report(tmp_path, ACTOR="octocat", ACTION_OUTCOME=outcome)
    assert run.returncode != 0
    assert "`NOT_EXECUTED`" in run.summary


# ── T05: the real binary, the repository's rules ──────────────────────

_REAL_GITLEAKS = shutil.which("gitleaks")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=witness", "-c", "user.email=witness@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    )


@pytest.mark.skipif(_REAL_GITLEAKS is None, reason="gitleaks binary not installed")
@pytest.mark.parametrize(
    ("content", "expected_code", "expected_result"),
    [
        ('dependencies = ["requests==2.32.0"]\n', 0, "EXECUTED_CLEAN"),
        # Synthetic: the AWS documentation example key ID shape, not a credential.
        ('aws_key = "' + "AKIA" + 'Q7W3E9R5T1Y8U2I6"\n', 1, "EXECUTED_FINDINGS"),
    ],
    ids=["clean", "synthetic-leak"],
)
def test_real_gitleaks_scans_a_dependabot_shaped_commit(
    tmp_path: Path, content: str, expected_code: int, expected_result: str
) -> None:
    """Real-binary witness: the Dependabot step detects a leak in a manifest bump."""
    assert _REAL_GITLEAKS is not None
    shutil.copy(REPO_ROOT / ".gitleaks.toml", tmp_path / ".gitleaks.toml")
    _git(tmp_path, "init", "-q")
    # pyproject.toml: a manifest Dependabot edits that .gitleaks.toml does not allowlist.
    (tmp_path / "pyproject.toml").write_text(content)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "chore(deps): bump")
    step = _step(GITLEAKS, "gitleaks", "Gitleaks scan (Dependabot events, binary)")
    real_dir = str(Path(_REAL_GITLEAKS).parent)
    run = _run_step(step, tmp_path, {"PATH": f"{real_dir}:/usr/bin:/bin"})
    assert run.returncode == expected_code, run.log
    assert f"result={expected_result}" in run.output
