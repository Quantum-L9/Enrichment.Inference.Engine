"""The refactoring gate does not run the catalog; ci.yml's deep plan owns it.

A pull request or push on this workflow used to run the full catalog with the
coverage floor. That catalog is the deep plan already in ci.yml (`make
test-deep`, step "Run the full catalog once"). This gate keeps ruff and mypy.
A missing database, peer, or Gate checkout stays NOT_RUN or a failure inside
the deep plan. This workflow must not report that catalog as passed.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "refactoring-validation.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
REPO_MK = ROOT / "Repo.mk"
CATALOG_STEP = "Run the full catalog once"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job() -> dict:
    jobs = _workflow()["jobs"]
    assert list(jobs) == ["refactor-gate"]
    return jobs["refactor-gate"]


def _recipe(target: str) -> str:
    source = REPO_MK.read_text(encoding="utf-8")
    match = re.search(
        rf"^{re.escape(target)}:.*\n(?P<recipe>(?:\t.*(?:\n|$))+)",
        source,
        flags=re.MULTILINE,
    )
    assert match, f"missing Repo.mk target {target!r}"
    return match.group("recipe")


def test_refactoring_gate_does_not_run_pytest() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "pytest" not in source
    assert "|| true" not in source
    assert "cov-fail-under" not in source
    assert "Run Full Test Suite" not in source
    job = _job()
    names = [step.get("name") for step in job["steps"]]
    assert "Run Ruff Linter (BLOCKING)" in names
    assert "Run Ruff Formatter Check (BLOCKING)" in names
    assert "Run Mypy Type Checker (BLOCKING)" in names
    for step in job["steps"]:
        run = step.get("run") or ""
        assert "pytest" not in run
        assert "||" not in run
        assert step.get("continue-on-error") is not True
        assert "if" not in step
    passed = next(step for step in job["steps"] if step.get("name") == "Refactoring Gate Passed")
    echo = passed["run"].lower()
    assert "catalog" not in echo
    assert "coverage" not in echo
    assert "passed the catalog" not in echo
    assert "safe to merge" not in echo


def test_deep_plan_owns_the_catalog_and_fails_closed() -> None:
    ci = yaml.safe_load(CI.read_text(encoding="utf-8"))
    owners = [
        (job_name, step)
        for job_name, job in ci["jobs"].items()
        for step in job.get("steps") or []
        if step.get("name") == CATALOG_STEP
    ]
    assert [job_name for job_name, _step in owners] == ["integration"]
    integration = ci["jobs"]["integration"]
    assert integration["needs"] == ["validate", "deep-admit"]
    assert "needs.deep-admit.outputs.run_heavy == 'true'" in integration["if"]
    catalog = owners[0][1]
    assert "if" not in catalog
    assert catalog.get("continue-on-error") is not True
    run = catalog["run"]
    assert "pytest tests/" in run
    assert "--cov-fail-under=" in run
    assert "|| true" not in run
    assert "||" not in run
    require = next(
        step
        for step in integration["steps"]
        if step.get("name") == "Require Redis, Neo4j, and pinned peers"
    )
    assert integration["steps"].index(require) < integration["steps"].index(catalog)
    require_run = require["run"]
    assert "NOT_RUN" in require_run
    assert "pinned CEG peer is missing" in require_run
    assert "pinned Gate peer is missing" in require_run
    assert "|| true" not in require_run
    recipe = _recipe("test-deep")
    assert "NOT_RUN" in recipe
    assert "--ignore=tests/integration" in recipe
    assert "|| true" not in recipe
    for step in _job()["steps"]:
        assert "make " not in (step.get("run") or "")
