"""WU-29 / F-038: `make pr` (local validation) references only scripts that exist."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT_RE = re.compile(r"(?<![\w/.-])((?:[\w-]+/)+[\w.-]+\.(?:py|sh))\b")

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")


def _dry_run(target: str, *extra: str) -> str:
    proc = subprocess.run(
        ["make", "-n", target, *extra],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.mark.parametrize("target", ["pr", "pr-guards", "agent-check"])
def test_every_script_a_validation_target_runs_exists(target: str) -> None:
    out = _dry_run(target, "PR_BASE=origin/main")
    scripts = {m for m in _SCRIPT_RE.findall(out) if not m.startswith((".venv/", "/"))}
    assert scripts, out
    missing = sorted(s for s in scripts if not (REPO_ROOT / s).is_file())
    assert missing == [], missing


def test_local_pr_never_publishes() -> None:
    """`make pr` is local validation; it is not the publication ceremony."""
    out = _dry_run("pr", "PR_BASE=origin/main")
    for forbidden in ("git push", "gh pr create", "gh pr merge", "pr_evaluate_and_merge"):
        assert forbidden not in out
