"""WU-29 / F-038: `make pr` resolves, validates, and publishes only through the
governance ceremony, against an explicit PR_BASE.

The publication negative controls run the real `make pr` against a FAKE
governance directory whose `pr` target only records that it was reached: no
test here can push or open a PR.
"""

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


@pytest.mark.parametrize("target", ["pr", "pr-check", "pr-guards", "agent-check"])
def test_every_script_a_pr_target_runs_exists(target: str) -> None:
    out = _dry_run(target, "PR_BASE=origin/main")
    scripts = {m for m in _SCRIPT_RE.findall(out) if not m.startswith((".venv/", "/"))}
    assert scripts, out
    missing = sorted(s for s in scripts if not (REPO_ROOT / s).is_file())
    assert missing == [], missing


def test_pr_check_never_publishes() -> None:
    out = _dry_run("pr-check", "PR_BASE=origin/main")
    for forbidden in ("git push", "gh pr", "make -C", "pr_evaluate_and_merge"):
        assert forbidden not in out


def test_pr_publishes_only_through_the_governance_ceremony() -> None:
    out = _dry_run("pr", "PR_BASE=origin/main", "GOV=/opt/gov")
    assert f'make -C "/opt/gov" pr WS="{REPO_ROOT}" PR_BASE="origin/main"' in out
    for forbidden in (
        "git push",
        "gh pr create",
        "gh pr merge",
        "pr_evaluate_and_merge",
        "--admin",
    ):
        assert forbidden not in out


# ── Publication negative controls (fake governance ceremony) ──────────────


def _fake_gov(tmp_path: Path) -> tuple[Path, Path]:
    gov = tmp_path / "gov"
    gov.mkdir()
    marker = tmp_path / "published"
    (gov / "Makefile").write_text(
        f'pr:\n\t@echo "WS=$(WS) PR_BASE=$(PR_BASE) PR_REMEDIATE=$(PR_REMEDIATE)" > {marker}\n'
    )
    return gov, marker


def _make_pr(tmp_path: Path, *args: str, path_prefix: Path | None = None):
    env = {
        "PATH": f"{path_prefix}:/usr/bin:/bin" if path_prefix else "/usr/bin:/bin",
        "HOME": str(tmp_path),
    }
    return subprocess.run(
        ["make", "pr", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_missing_base_publishes_nothing(tmp_path: Path) -> None:
    gov, marker = _fake_gov(tmp_path)
    proc = _make_pr(tmp_path, f"GOV={gov}")
    assert proc.returncode != 0
    assert "PR_BASE is required" in proc.stderr
    assert not marker.exists()


def test_unresolvable_base_publishes_nothing(tmp_path: Path) -> None:
    gov, marker = _fake_gov(tmp_path)
    proc = _make_pr(tmp_path, f"GOV={gov}", "PR_BASE=refs/heads/no-such-base-branch")
    assert proc.returncode != 0
    assert "does not resolve" in proc.stderr
    assert not marker.exists()


def test_missing_governance_ceremony_publishes_nothing(tmp_path: Path) -> None:
    proc = _make_pr(tmp_path, f"GOV={tmp_path / 'absent'}", "PR_BASE=HEAD")
    assert proc.returncode != 0
    assert "governance ceremony not found" in proc.stderr


def test_failed_validation_publishes_nothing(tmp_path: Path) -> None:
    """agent-check's first stage fails (a failing `ruff`), so nothing is handed off."""
    gov, marker = _fake_gov(tmp_path)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    ruff = stubs / "ruff"
    ruff.write_text("#!/bin/sh\necho 'lint failure' >&2\nexit 1\n")
    ruff.chmod(0o755)
    proc = _make_pr(tmp_path, f"GOV={gov}", "PR_BASE=HEAD", path_prefix=stubs)
    assert proc.returncode != 0
    assert "lint failure" in proc.stderr
    assert not marker.exists()


def test_validated_run_hands_off_once_with_the_explicit_base(tmp_path: Path) -> None:
    """With validation satisfied (`-o pr-check`: treated as already done), the run
    reaches the governance ceremony exactly once, with this workspace and the
    requested base, and opts out of remediation by default."""
    gov, marker = _fake_gov(tmp_path)
    proc = _make_pr(tmp_path, f"GOV={gov}", "PR_BASE=HEAD", "-o", "pr-check")
    assert proc.returncode == 0, proc.stderr
    assert marker.read_text().strip() == f"WS={REPO_ROOT} PR_BASE=HEAD PR_REMEDIATE=0"


def test_reentry_from_the_ceremony_is_refused(tmp_path: Path) -> None:
    gov, marker = _fake_gov(tmp_path)
    proc = subprocess.run(
        ["make", "pr", f"GOV={gov}", "PR_BASE=HEAD", "-o", "pr-check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "L9_EIE_PR_DELEGATED": "1"},
    )
    assert proc.returncode != 0
    assert "re-entry" in proc.stderr
    assert not marker.exists()
