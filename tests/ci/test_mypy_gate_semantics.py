"""Regression coverage for the unconditional MyPy merge gate."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# Target recipes live in Repo.mk; the root Makefile is Core's make-v1 facade.
MAKEFILE = REPO_ROOT / "Repo.mk"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _make_target_recipe(target: str) -> str:
    source = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(
        rf"^{re.escape(target)}:.*\n(?P<recipe>(?:\t.*(?:\n|$))+)",
        source,
        flags=re.MULTILINE,
    )
    assert match, f"missing Repo.mk target {target!r}"
    return match.group("recipe")


def _workflow_step(workflow: str, name: str) -> dict[str, object]:
    data = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    for job in data["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == name:
                return step
    raise AssertionError(f"{workflow}: no step {name!r}")


def test_make_type_check_has_no_advisory_bypass() -> None:
    recipe = _make_target_recipe("type-check")

    assert "$(PYTHON) -m mypy app --show-error-codes" in recipe
    assert "$(PYTHON) tools/mypy_ratchet.py" in recipe
    assert "set +e" not in recipe
    assert "||" not in recipe
    assert "PR_MYPY_STRICT" not in MAKEFILE.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
def test_make_type_check_propagates_a_mypy_failure(tmp_path: Path) -> None:
    failing_python = tmp_path / "failing-python"
    failing_python.write_text(
        "#!/bin/sh\n"
        "echo 'app/services/crm/field_mapper.py:1: error: simulated MyPy finding' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    failing_python.chmod(0o755)

    proc = subprocess.run(
        ["make", "type-check", f"PYTHON={failing_python}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0
    assert "simulated MyPy finding" in proc.stderr


@pytest.mark.parametrize(
    ("workflow", "step_name"),
    [
        ("ci.yml", "Run Mypy Type Checker"),
        ("pr-pipeline.yml", "Run Mypy type checker"),
    ],
)
def test_workflow_mypy_steps_have_no_nonblocking_shell_fallback(
    workflow: str, step_name: str
) -> None:
    step = _workflow_step(workflow, step_name)
    run = str(step["run"])

    assert "mypy" in run
    assert "||" not in run
    assert step.get("continue-on-error") is not True


def test_pr_pipeline_has_no_mypy_strict_opt_in() -> None:
    source = (WORKFLOWS / "pr-pipeline.yml").read_text(encoding="utf-8")

    assert "PR_MYPY_STRICT" not in source
    assert "mypy_strict" not in source
