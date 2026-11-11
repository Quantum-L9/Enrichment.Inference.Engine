"""Regression tests: CLI/file-derived values are validated before argv or file paths.

Each case pins a SonarCloud injection / path-traversal fix: option-like values,
shell metacharacters and ``../`` escapes are refused before any subprocess runs
or any file outside the repository is read.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from local_pr_pipeline import docs_link_check_local, run_pr_select_gates
from scripts import check_domain_compatibility as pair
from scripts import l9_contract_control
from tools import ai_review

ROOT = Path(__file__).resolve().parents[2]

HOSTILE_REVISIONS = [
    "-x",
    "--output=/tmp/pwned",
    "main..evil",
    "../etc/passwd",
    "HEAD;rm -rf /",
    "HEAD && id",
    "$(id)",
    "`id`",
    "HEAD|id",
    "a b",
    "HEAD\n",
    "",
]


def _load_classify_pr():
    spec = importlib.util.spec_from_file_location(
        "classify_pr_under_test", ROOT / ".github/scripts/classify_pr.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- git revisions (l9_contract_control / contract_bound_local) -------------


@pytest.mark.parametrize("value", ["f9bc5c2", "a" * 40, "origin/main", "HEAD~1", "HEAD^", "v1.2.3"])
def test_plain_git_revisions_are_accepted(value: str) -> None:
    assert l9_contract_control.validate_git_revision(value) == value


@pytest.mark.parametrize("value", HOSTILE_REVISIONS)
def test_hostile_git_revisions_are_refused(value: str) -> None:
    with pytest.raises(ValueError, match="invalid git revision"):
        l9_contract_control.validate_git_revision(value)


@pytest.mark.parametrize("value", ["-x", "--output=/tmp/pwned", "HEAD;id", "../x"])
def test_git_changed_files_refuses_before_running_git(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("git must not run for an invalid revision")

    monkeypatch.setattr(l9_contract_control.subprocess, "check_output", forbidden)
    with pytest.raises(ValueError):
        l9_contract_control.git_changed_files("HEAD", value)
    with pytest.raises(ValueError):
        l9_contract_control.git_changed_files(value, "HEAD")


def test_git_changed_files_ends_options_before_revisions(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake(command, **_kwargs):
        seen.append(command)
        return "a.py\n"

    monkeypatch.setattr(l9_contract_control.subprocess, "check_output", fake)
    assert l9_contract_control.git_changed_files("origin/main", "HEAD") == ["a.py"]
    assert seen == [["git", "diff", "--name-only", "--end-of-options", "origin/main", "HEAD", "--"]]


def test_contract_bound_local_uses_the_validated_helper() -> None:
    from local_pr_pipeline import contract_bound_local

    assert contract_bound_local.git_changed_files is l9_contract_control.git_changed_files


# --- run_pr_select_gates ------------------------------------------------------


@pytest.mark.parametrize("raw", ["../gates.json", "/etc/passwd", "docs/../../gates.json"])
def test_gates_file_outside_repo_is_refused(raw: str) -> None:
    with pytest.raises(SystemExit) as exc:
        run_pr_select_gates._confined_gates_path(raw)
    assert exc.value.code == 2


def test_gates_file_inside_repo_is_resolved() -> None:
    assert run_pr_select_gates._confined_gates_path("pyproject.toml") == ROOT / "pyproject.toml"


@pytest.mark.parametrize("command", ["rm -rf /", "pytest -q; id", "-x", "sh -c id"])
def test_undeclared_gate_command_never_runs(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    gates = ROOT / f".gates-under-test-{tmp_path.name}.json"
    gates.write_text(json.dumps({"gate_ids": ["x"], "commands": [command]}))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("an undeclared command must not run")

    monkeypatch.setattr(run_pr_select_gates.subprocess, "run", forbidden)
    monkeypatch.setattr(sys, "argv", ["run_pr_select_gates.py", str(gates)])
    try:
        assert run_pr_select_gates.main() == 2
    finally:
        gates.unlink()
    assert "not declared in the CI gate contract" in capsys.readouterr().out


def test_declared_gate_commands_are_allowed() -> None:
    declared = sorted(l9_contract_control.declared_gate_commands())
    assert declared
    assert run_pr_select_gates._undeclared_commands(declared) == []


# --- docs_link_check_local ------------------------------------------------------


@pytest.mark.parametrize("raw", ["../outside.md", "/etc/passwd", "docs/../../outside.md"])
def test_docs_link_check_refuses_paths_outside_repo(raw: str) -> None:
    with pytest.raises(ValueError, match="outside the repository"):
        docs_link_check_local._confined_path(raw)


def test_docs_link_check_main_exits_2_for_traversal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["docs_link_check_local.py", "../../etc/passwd"])
    assert docs_link_check_local.main() == 2


@pytest.mark.parametrize(
    ("target", "external"),
    [
        ("https://example.com", True),
        ("http://example.com", True),
        ("mailto:a@b.c", True),
        ("tel:123", True),
        ("//cdn.example.com/x", True),
        ("docs/README.md", False),
        ("../AGENTS.md", False),
        ("ftp://example.com", False),
    ],
)
def test_external_link_classification_is_unchanged(target: str, external: bool) -> None:
    assert docs_link_check_local._is_external(target) is external


# --- classify_pr ------------------------------------------------------------------


@pytest.mark.parametrize("value", ["-x", "--upload-pack=id", "abc;id", "a" * 39, "A" * 40, None, 7])
def test_classify_pr_refuses_non_sha_event_values(value: object) -> None:
    assert _load_classify_pr()._commit_sha(value) == ""


def test_classify_pr_hostile_event_never_reaches_git(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_classify_pr()

    def forbidden(_cmd):
        raise AssertionError("git must not run for a hostile event")

    monkeypatch.setattr(module, "_run", forbidden)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    event = {
        "pull_request": {"base": {"sha": "--upload-pack=id"}, "head": {"sha": "b" * 40}},
        "before": "-x",
        "after": "c" * 40,
    }
    assert module._changed_files(event) == ([], True)


def test_classify_pr_accepts_full_sha() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert _load_classify_pr()._commit_sha(sha) == sha


# --- tools/ai_review -----------------------------------------------------------------


@pytest.mark.parametrize("raw", ["../outside.py", "/etc/passwd", "app/../../outside.py"])
def test_ai_review_refuses_paths_outside_repo(raw: str) -> None:
    with pytest.raises(ValueError, match="outside the repository"):
        ai_review.confined_path(raw)
    with pytest.raises(ValueError, match="outside the repository"):
        ai_review.get_file_contents([raw])


def test_ai_review_reads_files_inside_repo() -> None:
    assert "# FILE: pyproject.toml" in ai_review.get_file_contents(["pyproject.toml"])


# --- scripts/check_domain_compatibility ----------------------------------------------


def _ceg_args(tmp_path: Path, *, python: Path | None = None) -> argparse.Namespace:
    repo = tmp_path / "ceg"
    (repo / "tools").mkdir(parents=True)
    (repo / "tools/validate_domain.py").write_bytes(b"# fixture\n")
    return argparse.Namespace(ceg_repo=repo, ceg_python=python or Path(sys.executable))


def test_ceg_inputs_are_absolute_and_never_option_like(tmp_path: Path) -> None:
    repo, python, validator = pair._trusted_ceg_inputs(_ceg_args(tmp_path))
    for value in (repo, python, validator):
        assert value.is_absolute()
        assert not str(value).startswith("-")


@pytest.mark.parametrize("name", ["-x", "--version", "python;id"])
def test_ceg_python_option_like_or_metachar_is_refused(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="--ceg-python"):
        pair._trusted_ceg_inputs(_ceg_args(tmp_path, python=Path(name)))


def test_ceg_python_must_be_executable(tmp_path: Path) -> None:
    stub = tmp_path / "python"
    stub.write_bytes(b"#!/bin/sh\n")
    stub.chmod(0o600)
    with pytest.raises(ValueError, match="executable"):
        pair._trusted_ceg_inputs(_ceg_args(tmp_path, python=stub))


def test_ceg_inputs_with_control_characters_are_refused(tmp_path: Path) -> None:
    args = _ceg_args(tmp_path)
    args.ceg_repo = Path(f"{args.ceg_repo}\n--evil")
    with pytest.raises(ValueError, match="control characters"):
        pair._trusted_ceg_inputs(args)


def test_ceg_validator_symlink_escaping_checkout_is_refused(tmp_path: Path) -> None:
    args = _ceg_args(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"# not in checkout\n")
    validator = args.ceg_repo / "tools/validate_domain.py"
    validator.unlink()
    validator.symlink_to(outside)
    with pytest.raises(ValueError, match="inside the checkout"):
        pair._trusted_ceg_inputs(args)


def test_ceg_missing_repo_is_refused(tmp_path: Path) -> None:
    args = argparse.Namespace(ceg_repo=tmp_path / "missing", ceg_python=Path(sys.executable))
    with pytest.raises(ValueError, match="--ceg-repo"):
        pair._trusted_ceg_inputs(args)
