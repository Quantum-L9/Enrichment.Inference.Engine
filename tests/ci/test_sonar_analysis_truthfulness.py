# --- L9_META ---
# l9_schema: 1
# origin: native
# engine: enrichment
# layer: [ci]
# tags: [sonarcloud, quality, regression]
# owner: platform
# status: active
# --- /L9_META ---
"""Sonar workflow contracts; local success is not a hosted analysis or gate PASS.

Only the real prerequisite shell is executed, with absent/blank credentials.
The scanner is never run, replaced with a stub, or given a synthetic gate result.
A missing SONAR_TOKEN fails that preflight. This job does not run pytest, so
the missing token cannot skip it, and the scanner does not run the full catalog.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/sonarcloud.yml"
NATIVE_ONLY = (
    "${{ github.event_name != 'pull_request' || "
    "github.event.pull_request.head.repo.full_name == github.repository }}"
)


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job() -> dict:
    return _workflow()["jobs"]["sonarqube"]


def _token_check() -> dict:
    checks = [
        step for step in _job()["steps"] if "run" in step and "SONAR_TOKEN" in step.get("env", {})
    ]
    assert len(checks) == 1, "Exactly one fail-closed credential preflight is required"
    return checks[0]


def _scan() -> dict:
    scans = [
        step
        for step in _job()["steps"]
        if step.get("uses", "").startswith("SonarSource/sonarqube-scan-action@")
    ]
    assert len(scans) == 1, "Use the native scanner, not a substitute success step"
    return scans[0]


def _analysis_properties() -> dict[str, str]:
    args = shlex.split(_scan()["with"]["args"])
    assert all(arg.startswith("-D") and "=" in arg for arg in args)
    properties = dict(arg[2:].split("=", 1) for arg in args)
    assert len(properties) == len(args), "Duplicate analysis properties can override the gate"
    return properties


CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
COVERAGE_ARTIFACT = "coverage-pr-fast"


def _ci_jobs() -> dict:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def test_only_approved_triggers_and_read_permissions_remain() -> None:
    """Sonar is called by ci.yml only, after the job that measures coverage."""
    workflow = _workflow()
    # PyYAML's YAML 1.1 resolver treats GitHub's unquoted `on` as True.
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert list(triggers) == ["workflow_call"]
    assert list(triggers["workflow_call"]["secrets"]) == ["SONAR_TOKEN"]
    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in _job()

    ci_triggers = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))[True]
    assert ci_triggers["push"] == {"branches": ["main"]}
    assert "pull_request" in ci_triggers
    caller = _ci_jobs()["sonar"]
    assert caller["uses"] == "./.github/workflows/sonarcloud.yml"
    assert caller["needs"] == "test"
    assert caller["secrets"] == {"SONAR_TOKEN": "${{ secrets.SONAR_TOKEN }}"}
    assert caller["permissions"] == {"contents": "read"}
    # Same events as the old standalone workflow: no deep-plan dispatch scan.
    assert caller["if"] == "${{ github.event_name != 'workflow_dispatch' }}"
    # Sonar is not a required check; ci-gate must not wait on it.
    assert "sonar" not in _ci_jobs()["ci-gate"]["needs"]


def test_coverage_comes_from_the_single_test_run_and_is_required() -> None:
    """0% coverage was imported because no job produced coverage.xml.

    The Test Suite job's one ``make test`` run writes it; Sonar downloads it and
    fails closed when it is absent. No second test run is made for Sonar.
    """
    test_steps = _ci_jobs()["test"]["steps"]
    runs = [step.get("run", "") for step in test_steps]
    assert sum(run.strip().startswith("make test") for run in runs) == 1
    assert "make test PR_FAST_COVERAGE_XML=coverage.xml" in runs
    upload = next(
        step for step in test_steps if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert upload["with"]["name"] == COVERAGE_ARTIFACT
    assert upload["with"]["path"] == "coverage.xml"
    assert upload["with"]["if-no-files-found"] == "error"
    assert test_steps.index(upload) > runs.index("make test PR_FAST_COVERAGE_XML=coverage.xml")

    steps = _job()["steps"]
    download = next(
        step for step in steps if step.get("uses", "").startswith("actions/download-artifact@")
    )
    assert download["with"] == {"name": "${{ inputs.coverage-artifact }}", "path": "."}
    inputs = _workflow()[True]["workflow_call"]["inputs"]
    assert inputs["coverage-artifact"]["default"] == COVERAGE_ARTIFACT
    assert inputs["coverage-artifact"]["type"] == "string"
    # The push/pull-request caller passes no artifact, so it gets PR-fast.
    assert "with" not in _ci_jobs()["sonar"]
    require = next(step for step in steps if "coverage.xml" in step.get("run", ""))
    assert "exit 1" in require["run"]
    assert steps.index(download) < steps.index(require) < steps.index(_scan())

    properties = (ROOT / "sonar-project.properties").read_text(encoding="utf-8")
    assert "sonar.python.coverage.reportPaths=coverage.xml" in properties


def test_pr_fast_coverage_is_opt_in_and_full_catalog_floor_is_unchanged() -> None:
    """Analyze (central Core) runs plain ``make test``; it must not change."""
    repo_mk = (ROOT / "Repo.mk").read_text(encoding="utf-8")
    assert "PR_FAST_COVERAGE_XML ?=\n" in repo_mk
    assert "$(if $(PR_FAST_COVERAGE_XML)," in repo_mk
    assert "COVERAGE_MIN ?= 71" in repo_mk
    assert "--cov-fail-under=$(COVERAGE_MIN)" in repo_mk


def test_fork_pull_requests_are_excluded_before_any_secret_step() -> None:
    job = _job()
    assert " ".join(job.get("if", "").split()) == NATIVE_ONLY
    assert "env" not in _workflow()
    assert not any("secrets." in str(value) for value in job.get("env", {}).values())
    assert "Fork pull requests" in WORKFLOW.read_text(encoding="utf-8")
    assert "not a quality-gate PASS" in WORKFLOW.read_text(encoding="utf-8")


def test_native_analysis_has_no_successful_conditional_skip_path() -> None:
    job = _job()
    assert "continue-on-error" not in job
    for step in job["steps"]:
        assert "if" not in step, f"Native prerequisite/analysis may not be skipped: {step['name']}"
        assert "continue-on-error" not in step
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "steps.gate.outputs.run" not in source
    assert "run=false" not in source
    assert "::notice::SonarCloud skipped" not in source


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.parametrize(
    "token", [None, "", " ", "\t\n"], ids=["absent", "empty", "space", "whitespace"]
)
def test_missing_or_blank_token_fails_instead_of_reporting_success(
    tmp_path: Path, token: str | None
) -> None:
    check = _token_check()
    assert "${{" not in check["run"], "Secret material must enter via env, not shell interpolation"
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(output)}
    if token is not None:
        env["SONAR_TOKEN"] = token
    shell = ["bash", "--noprofile", "--norc", "-eo", "pipefail"]
    result = subprocess.run(
        [*shell, "-c", check["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode != 0, "Missing analysis authority must fail, never succeed as a skip"
    assert "::error::" in result.stdout + result.stderr
    assert "SONAR_TOKEN" in result.stdout + result.stderr
    assert "run=true" not in output.read_text(encoding="utf-8")
    assert "::notice::" not in result.stdout + result.stderr


def test_token_preflight_is_first_and_secret_is_not_echoed() -> None:
    check = _token_check()
    assert _job()["steps"][0] == check
    assert check["env"] == {"SONAR_TOKEN": "${{ secrets.SONAR_TOKEN }}"}
    assert check.get("shell") == "bash"
    assert "exit 1" in check["run"]
    assert not re.search(r"(?:echo|printf).*\$(?:\{)?SONAR_TOKEN", check["run"])


@pytest.mark.parametrize(
    ("property_name", "expected"),
    [
        ("sonar.organization", "quantum-l9"),
        ("sonar.projectKey", "Quantum-L9_Enrichment.Inference.Engine"),
    ],
)
def test_scan_uses_public_api_verified_native_coordinates(
    property_name: str, expected: str
) -> None:
    assert _analysis_properties()[property_name] == expected
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "vars.SONAR_ORG_KEY" not in source
    assert "vars.SONAR_PROJECT_KEY" not in source


def test_native_scanner_waits_for_actual_quality_gate_and_propagates_failure() -> None:
    scan = _scan()
    properties = _analysis_properties()
    assert properties["sonar.qualitygate.wait"] == "true"
    assert properties["sonar.qualitygate.timeout"] == "300"
    # Pinned to the full commit SHA of the reviewed v8 release (SonarCloud
    # githubactions:S7637); the trailing comment names the tag it resolves to.
    assert scan["uses"] == (
        "SonarSource/sonarqube-scan-action@e805d875275f728fac1885b413934be6d7c63f65"
    )
    assert (
        "uses: SonarSource/sonarqube-scan-action@e805d875275f728fac1885b413934be6d7c63f65 # v8.4.1"
        in WORKFLOW.read_text(encoding="utf-8")
    )
    assert scan["env"] == {"SONAR_TOKEN": "${{ secrets.SONAR_TOKEN }}"}
    assert "if" not in scan
    assert "continue-on-error" not in scan
    assert _job()["steps"][-1] == scan


def test_checkout_is_full_current_head_without_persisted_credentials() -> None:
    checkout = next(
        step for step in _job()["steps"] if step.get("uses", "").startswith("actions/checkout@")
    )
    # actions/checkout v7.0.1, pinned by commit SHA (the tag is the trailing comment in the workflow).
    assert checkout["uses"] == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}


def test_missing_token_does_not_skip_pytest_and_scanner_does_not_run_the_catalog() -> None:
    """Pytest stays outside this job. A missing token fails the scan only."""
    job = _job()
    assert "services" not in job
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "pytest" not in source
    assert "TEST_DIR" not in source
    assert "-n auto" not in source
    assert "Tests with coverage (Cobertura XML)" not in source
    token_at = job["steps"].index(_token_check())
    for step in job["steps"][token_at:]:
        assert "pytest" not in step.get("run", "")
        assert "pytest" not in step.get("with", {}).get("args", "")
    assert "|| true" not in source


FULL_ARTIFACT = "coverage-full"
DISPATCHED = "a" * 40


def test_deep_catalog_writes_and_uploads_full_coverage_from_its_single_run() -> None:
    """(1) The deep plan's one full-catalog run is the main-branch coverage source."""
    steps = _ci_jobs()["integration"]["steps"]
    catalog = [step for step in steps if "pytest tests/ \\" in step.get("run", "")]
    assert len(catalog) == 1, "exactly one full-catalog pytest run"
    assert "--cov-report=xml:coverage.xml" in catalog[0]["run"]
    assert "--cov-fail-under=${{ env.COVERAGE_THRESHOLD }}" in catalog[0]["run"]
    upload = next(
        step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert upload["with"]["name"] == FULL_ARTIFACT
    assert upload["with"]["path"] == "coverage.xml"
    assert upload["with"]["if-no-files-found"] == "error"
    assert steps.index(upload) == steps.index(catalog[0]) + 1


def test_main_analysis_runs_only_after_a_successful_deep_apply_with_full_coverage() -> None:
    """(2) Same reusable workflow, fed coverage-full, gated on the deep run."""
    jobs = _ci_jobs()
    admit = jobs["deep-sonar-admit"]
    assert admit["needs"] == ["deep-admit", "integration"]
    assert admit["if"] == "${{ always() && needs.integration.result == 'success' }}"
    deep = jobs["sonar-deep"]
    assert deep["uses"] == "./.github/workflows/sonarcloud.yml"
    assert deep["with"] == {"coverage-artifact": FULL_ARTIFACT}
    assert deep["needs"] == "deep-sonar-admit"
    assert deep["if"] == "${{ always() && needs.deep-sonar-admit.outputs.run == 'true' }}"
    assert deep["secrets"] == {"SONAR_TOKEN": "${{ secrets.SONAR_TOKEN }}"}
    for name in ("sonar", "deep-sonar-admit", "sonar-deep"):
        assert name not in jobs["ci-gate"]["needs"]


def _run_admission(tmp_path: Path, *, main_head: str, ref: str, github_sha: str) -> tuple:
    """Execute the real admission shell; only the GitHub API read is stubbed."""
    step = _ci_jobs()["deep-sonar-admit"]["steps"][0]
    assert "${{" not in step["run"], "inputs must enter through env, not interpolation"
    stub = tmp_path / "bin"
    stub.mkdir()
    gh = stub / "gh"
    gh.write_text(f'#!/bin/sh\necho "{main_head}"\n', encoding="utf-8")
    gh.chmod(0o755)
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    env = {
        "PATH": f"{stub}:/usr/bin:/bin",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_REPOSITORY": "Quantum-L9/Enrichment.Inference.Engine",
        "GITHUB_REF": ref,
        "GITHUB_SHA": github_sha,
        "EXPECTED_SHA": DISPATCHED,
        "DEFAULT_BRANCH": "main",
        "GH_TOKEN": "not-a-real-token",
    }
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return result, output.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_main_analysis_is_admitted_when_main_head_is_the_dispatched_revision(
    tmp_path: Path,
) -> None:
    result, output = _run_admission(
        tmp_path, main_head=DISPATCHED, ref="refs/heads/main", github_sha=DISPATCHED
    )
    assert result.returncode == 0, result.stderr
    assert "run=true" in output
    assert "NOT_RUN" not in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.parametrize(
    ("main_head", "ref", "github_sha"),
    [
        ("b" * 40, "refs/heads/main", DISPATCHED),
        (DISPATCHED, "refs/heads/feature", DISPATCHED),
        (DISPATCHED, "refs/heads/main", "c" * 40),
    ],
    ids=["main-moved", "not-main", "run-sha-differs"],
)
def test_main_analysis_is_not_run_when_main_head_is_not_the_dispatched_revision(
    tmp_path: Path, main_head: str, ref: str, github_sha: str
) -> None:
    """(3) Coverage must never be attached to a revision it did not measure."""
    result, output = _run_admission(tmp_path, main_head=main_head, ref=ref, github_sha=github_sha)
    assert result.returncode == 0, result.stderr
    assert "run=false" in output
    assert "run=true" not in output
    assert "NOT_RUN:" in result.stdout


def test_every_declared_sonar_source_directory_exists() -> None:
    """SonarScanner aborts on a missing ``sonar.sources`` entry (``engine`` did)."""
    properties = {}
    for line in (ROOT / "sonar-project.properties").read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            properties[key.strip()] = value.strip()
    for name in ("sonar.sources", "sonar.tests"):
        entries = [entry.strip() for entry in properties[name].split(",") if entry.strip()]
        assert entries, f"{name} must name at least one directory"
        missing = [entry for entry in entries if not (ROOT / entry).is_dir()]
        assert missing == [], f"{name} names directories that do not exist: {missing}"
