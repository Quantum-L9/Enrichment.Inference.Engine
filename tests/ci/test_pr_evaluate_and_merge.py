"""WU-08 / F-006: the merge driver is CLEAN only when CI, required checks and the
review gate all pass.

Every GitHub and merge effect is stubbed: `gh` is replaced by a function that
fails the run if called, and `do_merge` only records that it was reached. No
real PR is read, evaluated or merged.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "pr_evaluate_and_merge.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")

_CHECKS = (
    "run_id_for_head",
    "wait_for_workflow_run",
    "verify_pr_pipeline_checks",
    "verify_required_checks",
    "verify_review_gate",
)


def _harness(failing: str | None, body: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    stubs = "\n".join(
        f"{name}() {{ echo STEP:{name}; return {1 if name == failing else 0}; }}"
        for name in _CHECKS
    )
    script = f"""
set -euo pipefail
source "{SCRIPT}" merge 7
gh() {{ echo "UNEXPECTED gh $*" >&2; exit 97; }}
require_gh() {{ :; }}
resolve_pr() {{ echo 7; }}
branch_name() {{ echo feature; }}
head_sha() {{ echo 0123456789abcdef; }}
wait_for_new_commit() {{ return 1; }}
do_merge() {{ echo DO_MERGE_CALLED; }}
{stubs}
{body}
"""
    # A `gh` executable stub first on PATH as well as the shell function: even
    # code that runs before the function is defined can never reach real GitHub.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text('#!/bin/sh\necho "UNEXPECTED gh $*" >&2\nexit 97\n')
    fake_gh.chmod(0o755)
    return subprocess.run(
        ["bash", "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "PR_MERGE_WATCH": "0", "HOME": str(tmp_path)},
    )


@pytest.mark.parametrize(
    "failing", ["verify_pr_pipeline_checks", "verify_required_checks", "wait_for_workflow_run"]
)
def test_a_failing_check_with_a_clean_review_gate_is_not_clean(
    failing: str, tmp_path: Path
) -> None:
    proc = _harness(failing, "main", tmp_path)
    assert proc.returncode != 0
    assert "DO_MERGE_CALLED" not in proc.stdout
    assert "not CLEAN" in proc.stderr
    assert "UNEXPECTED gh" not in proc.stderr


@pytest.mark.parametrize("failing", _CHECKS)
def test_evaluate_once_returns_non_zero_for_any_failed_step(failing: str, tmp_path: Path) -> None:
    proc = _harness(failing, "if evaluate_once 7; then echo RC=0; else echo RC=1; fi", tmp_path)
    assert "RC=1" in proc.stdout, proc.stdout + proc.stderr


def test_all_pass_still_reaches_do_merge(tmp_path: Path) -> None:
    proc = _harness(None, "main", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "DO_MERGE_CALLED" in proc.stdout
    assert all(f"STEP:{name}" in proc.stdout for name in _CHECKS[1:])


def test_sourcing_the_script_does_not_run_main(tmp_path: Path) -> None:
    proc = _harness(None, "echo SOURCED_ONLY", tmp_path)
    assert proc.stdout.strip().endswith("SOURCED_ONLY")
    assert "PR merge gate" not in proc.stdout
