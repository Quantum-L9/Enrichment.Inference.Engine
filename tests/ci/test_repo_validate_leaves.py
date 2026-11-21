"""`make validate` leaves added for the EIE org-CI cutover (Phase 1 rows 11, 18, 20, 28, 31).

`make validate` runs inside the required `Analyze (central Core)` check, so a
leaf removed from the `repo-validate` recipe silently drops that protection
from merge. The recipe test pins each leaf; the KB rule and workflow YAML
checks get good and bad witnesses here because their logic is new in tools/.
The other leaves call existing scripts whose own contract tests stay where
they are; their negative witnesses run on the cutover canary PR.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.validate_kb_rules import kb_rule_errors
from tools.validate_workflow_yaml import workflow_errors

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

REQUIRED_LEAVES = (
    "tools/verify_contracts.py",
    "tools/payload_contract_compiler.py --stdout-only",
    "scripts/validate_sdk_pin.py --verify-tag",
    "tools/audit_engine.py --strict",
    "tools/validate_kb_rules.py",
    "local_pr_pipeline/check_compliance_terminology.py",
    "scripts/verify_node_constitution.py",
    "scripts/l9_contract_control.py verify-constitution",
    "scripts/l9_contract_control.py verify-attestation",
    "tools/validate_workflow_yaml.py",
)


def _repo_validate_recipe() -> str:
    text = (REPO_ROOT / "Repo.mk").read_text(encoding="utf-8")
    match = re.search(r"^repo-validate:.*\n((?:\t.*\n)+)", text, flags=re.MULTILINE)
    assert match, "repo-validate target not found in Repo.mk"
    return match.group(1)


def _ignores_errors(recipe_line: str) -> bool:
    """Whether make ignores this command's exit status: `-` anywhere in its prefix."""
    prefix = re.match(r"[@+\-\s]*", recipe_line.lstrip("\t"))
    return prefix is not None and "-" in prefix.group(0)


@pytest.mark.parametrize(
    ("line", "ignored"),
    [
        ("\t$(PYTHON) tools/x.py", False),
        ("\t@$(PYTHON) tools/x.py", False),
        ("\t+$(PYTHON) tools/x.py", False),
        ("\t-$(PYTHON) tools/x.py", True),
        ("\t@-$(PYTHON) tools/x.py", True),
        ("\t-@$(PYTHON) tools/x.py", True),
        ("\t@ - $(PYTHON) tools/x.py", True),
    ],
)
def test_ignored_error_prefix_is_detected(line: str, ignored: bool) -> None:
    assert _ignores_errors(line) is ignored


@pytest.mark.parametrize("leaf", REQUIRED_LEAVES)
def test_repo_validate_runs_the_leaf(leaf: str) -> None:
    lines = [line for line in _repo_validate_recipe().splitlines() if line.rstrip().endswith(leaf)]
    assert lines, f"`make validate` no longer runs {leaf}"
    assert not any(_ignores_errors(line) for line in lines), (
        f"`make validate` ignores the exit status of {leaf}"
    )


# ── KB rule schema ─────────────────────────────────────────────────────


def _kb(tmp_path: Path, body: str) -> Path:
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "rules.yaml").write_text(body, encoding="utf-8")
    return kb


def test_kb_conforming_rules_pass(tmp_path: Path) -> None:
    kb = _kb(
        tmp_path, "rules:\n  - field: mfi\n    when: {gt: 1}\n  - field: x\n    conditions: []\n"
    )
    assert kb_rule_errors(kb) == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("rules:\n  - when: {gt: 1}\n", "missing required key: field"),
        ("rules:\n  - field: mfi\n", "missing conditions/when block"),
        ("- a\n- b\n", "Root must be a YAML mapping"),
        ("rules: {field: mfi}\n", "`rules` must be a list"),
        ("rules:\n  - just-a-string\n", "must be a mapping"),
        ("rules: [\n", "while parsing"),
        ("rules:\n  - when: {gt: 1}\nrules: []\n", "found duplicate key 'rules'"),
    ],
)
def test_kb_violations_are_reported(tmp_path: Path, body: str, expected: str) -> None:
    errors = kb_rule_errors(_kb(tmp_path, body))
    assert len(errors) == 1
    assert expected in errors[0]


def test_kb_merge_key_override_is_not_a_duplicate(tmp_path: Path) -> None:
    body = "base: &b {field: x, when: {gt: 1}}\nrules:\n  - <<: *b\n    field: y\n"
    assert kb_rule_errors(_kb(tmp_path, body)) == []


def test_kb_absent_passes(tmp_path: Path) -> None:
    assert kb_rule_errors(tmp_path / "kb") == []


def test_kb_of_this_repository_conforms() -> None:
    assert kb_rule_errors(REPO_ROOT / "kb") == []


# ── Workflow YAML ──────────────────────────────────────────────────────


def _workflows(tmp_path: Path, body: str) -> Path:
    directory = tmp_path / "workflows"
    directory.mkdir()
    (directory / "ci.yml").write_text(body, encoding="utf-8")
    return directory


def test_workflow_that_parses_passes(tmp_path: Path) -> None:
    directory = _workflows(tmp_path, "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n")
    assert workflow_errors(directory) == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "on: push\njobs:\n  a:\n    steps:\n      - run: echo\n     - run: bad\n",
            "while parsing",
        ),
        ("on: push\n", "no `jobs` mapping"),
        ("- a\n", "root must be a YAML mapping"),
        ("on: push\njobs: 1\njobs:\n  a:\n    runs-on: x\n", "found duplicate key 'jobs'"),
    ],
)
def test_broken_workflow_is_reported(tmp_path: Path, body: str, expected: str) -> None:
    errors = workflow_errors(_workflows(tmp_path, body))
    assert len(errors) == 1
    assert expected in errors[0]


def test_no_workflows_is_reported(tmp_path: Path) -> None:
    assert workflow_errors(tmp_path) != []


def test_workflows_of_this_repository_parse() -> None:
    assert workflow_errors(REPO_ROOT / ".github" / "workflows") == []
