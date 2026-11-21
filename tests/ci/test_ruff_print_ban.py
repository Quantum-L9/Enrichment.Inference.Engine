"""No `print(` in service code, enforced by ruff T20 (EIE org-CI cutover, Phase 1 row 29).

compliance.yml grepped app/ and engine/ for `print(`, but that workflow is not
a required check. `make check` (Repo.mk `repo-check`), which the required
`Analyze (central Core)` check runs, now runs `ruff check --select T20` over
app/ and engine/ only. That leaves pyproject.toml's ruff select and its frozen
ignore list (C-18 / INV-17) untouched, and CLIs, tools, scripts and tests may
print. The witness runs the real ruff with the recipe's own command.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _repo_check_recipe() -> str:
    text = (REPO_ROOT / "Repo.mk").read_text(encoding="utf-8")
    match = re.search(r"^repo-check:.*\n((?:\t.*\n)+)", text, flags=re.MULTILINE)
    assert match, "repo-check target not found in Repo.mk"
    return match.group(1)


def test_repo_check_runs_the_print_ban_over_service_code() -> None:
    text = (REPO_ROOT / "Repo.mk").read_text(encoding="utf-8")
    assert re.search(r"^PRINT_BAN_PATHS := \$\(wildcard app engine\)$", text, re.MULTILINE)
    lines = [line.strip() for line in _repo_check_recipe().splitlines()]
    assert "$(PYTHON) -m ruff check --select T20 --no-fix --ignore-noqa $(PRINT_BAN_PATHS)" in lines


def test_the_frozen_ruff_configuration_is_not_used_for_it() -> None:
    """C-18: the ban lives in the recipe, not in pyproject's select or ignores."""
    lint = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"][
        "ruff"
    ]["lint"]
    assert "T20" not in lint["select"]
    assert not any("T20" in rules for rules in lint.get("per-file-ignores", {}).values())


def test_print_in_service_code_is_a_finding_and_elsewhere_is_not(tmp_path: Path) -> None:
    for relative, body in {
        "app/svc.py": 'def f() -> None:\n    print("x")\n',
        "engine/svc.py": 'def f() -> None:\n    print("x")\n',
        "app/suppressed.py": 'def f() -> None:\n    print("x")  # noqa: T201\n',
        "tools/cli.py": 'print("ok")\n',
        "tests/test_cli.py": 'print("ok")\n',
    }.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    # The recipe's command, with PRINT_BAN_PATHS expanded for this tree.
    proc = subprocess.run(
        [
            *(
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--select",
                "T20",
                "--no-fix",
                "--ignore-noqa",
            ),
            *("--output-format", "concise", "app", "engine"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    flagged = {line.split(":", 1)[0] for line in proc.stdout.splitlines() if "T201" in line}
    assert flagged == {"app/svc.py", "app/suppressed.py", "engine/svc.py"}, proc.stdout


def test_service_code_of_this_repository_has_no_print() -> None:
    paths = [name for name in ("app", "engine") if (REPO_ROOT / name).is_dir()]
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "T20",
            "--no-fix",
            "--ignore-noqa",
            *paths,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
