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


def test_only_approved_triggers_and_read_permissions_remain() -> None:
    workflow = _workflow()
    # PyYAML's YAML 1.1 resolver treats GitHub's unquoted `on` as True.
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert triggers == {
        "push": {"branches": ["main"]},
        "pull_request": {"types": ["opened", "synchronize", "reopened"]},
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in _job()


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
    assert checkout["uses"] == "actions/checkout@v7"
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
