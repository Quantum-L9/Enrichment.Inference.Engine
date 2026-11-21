"""No `print(` in service code, enforced by ruff T20 (EIE org-CI cutover, Phase 1 row 29).

compliance.yml grepped app/ and engine/ for `print(`, but that workflow is not
a required check. Ruff's T20 runs in `make check`, which the required
`Analyze (central Core)` check runs. The negated per-file ignore keeps T20 off
CLIs, tools, scripts and tests, which print by design. The witness runs the
real ruff against this repository's configuration in a scratch project.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PATTERN = "!{app,engine}/**.py"


def _lint() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"][
        "ruff"
    ]["lint"]


def test_t20_is_selected_and_scoped_to_service_code() -> None:
    lint = _lint()
    assert "T20" in lint["select"]
    assert lint["per-file-ignores"].get(PATTERN) == ["T20"]
    assert "T20" not in lint.get("ignore", [])


def test_print_in_service_code_is_a_finding_and_in_tools_is_not(tmp_path: Path) -> None:
    shutil.copy(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    for relative, body in {
        "app/svc.py": 'def f() -> None:\n    print("x")\n',
        "engine/svc.py": 'def f() -> None:\n    print("x")\n',
        "tools/cli.py": 'print("ok")\n',
        "tests/test_cli.py": 'print("ok")\n',
    }.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            ".",
            "--select",
            "T20",
            "--output-format",
            "concise",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    flagged = {line.split(":", 1)[0] for line in proc.stdout.splitlines() if "T201" in line}
    assert flagged == {"app/svc.py", "engine/svc.py"}, proc.stdout + proc.stderr
