"""WU-24: workflow gate steps propagate failure; advisory classification is rule-based.

F-030: a gate piped into `tee` must fail its step. GitHub runs an unspecified
`shell` as `bash -e {0}` (no pipefail) and `shell: bash` as
`bash --noprofile --norc -eo pipefail {0}` (workflow syntax reference,
`jobs.<job_id>.steps[*].shell`).
F-034: advisory vs blocking depends on the finding's rule, not message text.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

_SHELL_TEMPLATES = {
    None: ["bash", "-e"],
    "bash": ["bash", "--noprofile", "--norc", "-eo", "pipefail"],
}


def _steps(workflow: str) -> list[dict]:
    data = yaml.safe_load((WORKFLOWS / workflow).read_text())
    return [step for job in data["jobs"].values() for step in job.get("steps", [])]


def _step(workflow: str, name: str) -> dict:
    for step in _steps(workflow):
        if step.get("name") == name:
            return step
    raise AssertionError(f"{workflow}: no step {name!r}")


@pytest.mark.parametrize(
    "workflow", ["l9-contract-control.yml", "perplexity-code-review.yml", "audit-pr-review.yml"]
)
def test_every_step_that_pipes_into_tee_runs_with_pipefail(workflow: str) -> None:
    for step in _steps(workflow):
        run = step.get("run") or ""
        if re.search(r"\|\s*tee\b", run):
            assert step.get("shell") == "bash", f"{workflow}: {step.get('name')}"


def _run_step(step: dict, tmp_path: Path, stub_exit: int) -> tuple[int, str]:
    """Execute a step's script under its declared shell with a stub `python3`/`python`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("python", "python3"):
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\necho stub review output\nexit {stub_exit}\n")
        stub.chmod(0o755)
    script = re.sub(r"\$\{\{[^}]*\}\}", "false", step["run"])
    script_path = tmp_path / "step.sh"
    script_path.write_text(script)
    output = tmp_path / "github_output"
    output.write_text("")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }
    proc = subprocess.run(
        [*_SHELL_TEMPLATES[step.get("shell")], str(script_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, output.read_text()


pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")


def test_review_signal_step_fails_when_review_signal_blocks(tmp_path: Path) -> None:
    """Negative control: the piped `review-signal` exits 1 -> the step fails."""
    step = _step("l9-contract-control.yml", "Emit review signal")
    returncode, _ = _run_step(step, tmp_path, stub_exit=1)
    assert returncode != 0


def test_review_signal_step_passes_when_review_signal_passes(tmp_path: Path) -> None:
    step = _step("l9-contract-control.yml", "Emit review signal")
    returncode, _ = _run_step(step, tmp_path, stub_exit=0)
    assert returncode == 0


def test_ai_review_exit_1_is_reported_as_exit_code_1(tmp_path: Path) -> None:
    step = _step("perplexity-code-review.yml", "Run AI Review")
    (tmp_path / "pr_diff.txt").write_text("diff")
    _, output = _run_step(step, tmp_path, stub_exit=1)
    assert "exit_code=1" in output


def test_ai_review_exit_0_is_reported_as_exit_code_0(tmp_path: Path) -> None:
    step = _step("perplexity-code-review.yml", "Run AI Review")
    (tmp_path / "pr_diff.txt").write_text("diff")
    _, output = _run_step(step, tmp_path, stub_exit=0)
    assert "exit_code=0" in output


# ── F-034 ─────────────────────────────────────────────────────────────


def _audit_policy() -> dict:
    """Load the classification functions from the workflow's inline script."""
    run = _step("audit-pr-review.yml", "Run Audit Engine")["run"]
    source = run.split("python3 << 'PY'\n", 1)[1].split("\nPY", 1)[0]
    definitions = source.split("\ntry:\n    raw_findings", 1)[0]
    namespace: dict = {}
    exec(compile(definitions, "audit-pr-review.yml", "exec"), namespace)
    return namespace


@pytest.mark.parametrize("identifier", ["fontStyle", "namingScheme", "lowConfidence"])
def test_identifier_text_does_not_change_classification(identifier: str) -> None:
    policy = _audit_policy()
    finding = {"rule": 1, "severity": "CRITICAL", "message": f"camelCase field '{identifier}'"}
    control = {"rule": 1, "severity": "CRITICAL", "message": "camelCase field 'colorCode'"}
    assert policy["is_advisory_category"](finding) == policy["is_advisory_category"](control)
    assert policy["is_advisory_category"](finding) is False


def test_rule_based_advisory_still_applies() -> None:
    policy = _audit_policy()
    finding = {
        "rule": 2,
        "severity": "CRITICAL",
        "message": "Likely flatcase field 'abcdefghijklmn'",
    }
    assert policy["is_advisory_category"](finding) is True
