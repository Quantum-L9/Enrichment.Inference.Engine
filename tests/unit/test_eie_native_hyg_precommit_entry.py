"""The pytest-unit pre-commit hook covers every test path under tests/ (L3-HYG).

The hook's selection is spelled out literally so the Core pull-request
test-expense judge can read it: a shell-wrapped entry, or a bare ``tests/``,
is judged "no literal entry" or a full run. Listing paths can drift when a new
directory is added, so these tests keep the literal list equal to the set of
``tests/`` children that hold test files, and keep the hook out of CI.

L9_META:
  tier: 2
  domain: ci
  authority: EIE-NATIVE-CLOSURE-001 L3-HYG (CTO ANSWER, option A)
"""

from __future__ import annotations

import configparser
import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"


def _hook() -> dict[str, Any]:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [hook for repo in config["repos"] for hook in repo.get("hooks", [])]
    (hook,) = [hook for hook in hooks if hook.get("id") == "pytest-unit"]
    assert isinstance(hook, dict)
    return hook


def _test_file_pattern() -> str:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ROOT / "pytest.ini", encoding="utf-8")
    return parser.get("pytest", "python_files", fallback="test_*.py").split()[0]


def _expected_paths() -> set[str]:
    pattern = _test_file_pattern()
    dirs = {
        f"tests/{child.name}"
        for child in TESTS.iterdir()
        if child.is_dir() and any(child.rglob(pattern))
    }
    files = {f"tests/{child.name}" for child in TESTS.glob(pattern) if child.is_file()}
    return dirs | files


def test_entry_is_a_literal_unit_marker_pytest_run() -> None:
    hook = _hook()
    argv = shlex.split(hook["entry"])
    assert argv[:3] == ["python3", "-m", "pytest"]
    assert argv[argv.index("-m", 2) + 1] == "unit"
    assert "-c" not in argv
    assert hook.get("pass_filenames") is False
    assert hook.get("language") == "system"
    assert "|| true" not in hook["entry"]


def test_selection_lists_every_tests_child_that_holds_tests() -> None:
    listed = list(_hook().get("args", []))
    assert len(listed) == len(set(listed)), "a path is listed twice"
    missing = _expected_paths() - set(listed)
    assert not missing, f"add to the pytest-unit hook args: {sorted(missing)}"
    stale = set(listed) - _expected_paths()
    assert not stale, f"remove from the pytest-unit hook args: {sorted(stale)}"


@pytest.mark.parametrize("token", ["", ".", "tests", "tests/", "./tests"])
def test_selection_is_never_the_bare_tests_root(token: str) -> None:
    assert token not in _hook().get("args", [])


def test_no_workflow_runs_pre_commit_hooks() -> None:
    """The hook is local protection; no CI job may run it as a second catalog."""
    invocation = re.compile(r"pre-commit\s+(?:run|try-repo)|pre-commit/action@")
    runners = [
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / ".github/workflows").glob("*.y*ml"))
        if invocation.search(path.read_text(encoding="utf-8"))
    ]
    for make in ("Makefile", "Repo.mk"):
        if invocation.search((ROOT / make).read_text(encoding="utf-8")):
            runners.append(make)
    assert runners == []
