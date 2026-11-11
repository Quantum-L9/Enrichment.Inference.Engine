"""Unit tests for the EIE_REQUIRE_INTEGRATION guard in tests/conftest.py.

CI's `integration` job (.github/workflows/ci.yml) runs tests/integration against
real Redis and Neo4j Enterprise services and pinned Constellation.Gate /
Cognitive.Engine.Graphs checkouts with EIE_REQUIRE_INTEGRATION=1. Under that
flag a missing service must FAIL the job instead of skipping it, otherwise the
job reports green while exercising nothing. Without the flag, skip behaviour is
unchanged. Port of Cognitive.Engine.Graphs' CEG_REQUIRE_INTEGRATION guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests import conftest as root_conftest

pytestmark = pytest.mark.unit

_ENV = root_conftest.REQUIRE_INTEGRATION_ENV
_CI = root_conftest.REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _skipped_report(path: Path, *, wasxfail: str | None = None) -> pytest.TestReport:
    report = pytest.TestReport(
        nodeid=f"{path.name}::test_x",
        location=(str(path), 1, "test_x"),
        keywords={},
        outcome="skipped",
        longrepr=(str(path), 1, "Skipped: no Redis at REDIS_URL"),
        when="setup",
    )
    if wasxfail is not None:
        report.wasxfail = wasxfail
    return report


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " 1 "])
def test_integration_required_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(_ENV, value)
    assert root_conftest.integration_required() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "2"])
def test_integration_required_falsy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(_ENV, value)
    assert root_conftest.integration_required() is False


def test_integration_required_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    assert root_conftest.integration_required() is False


def test_unavailable_service_skips_without_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception, match="REDIS_URL unreachable"):
        root_conftest.skip_or_fail_unavailable("REDIS_URL unreachable")


def test_unavailable_service_fails_with_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    with pytest.raises(
        pytest.fail.Exception, match=r"EIE_REQUIRE_INTEGRATION=1 but REDIS_URL unreachable"
    ):
        root_conftest.skip_or_fail_unavailable("REDIS_URL unreachable")


def test_integration_path_classification() -> None:
    integration_file = root_conftest.INTEGRATION_DIR / "test_redis_idempotency_tenant_isolation.py"
    unit_file = Path(__file__)
    assert root_conftest._is_integration_path(integration_file) is True
    assert root_conftest._is_integration_path(unit_file) is False


def test_skip_report_is_rewritten_to_failure() -> None:
    report = _skipped_report(root_conftest.INTEGRATION_DIR / "test_converge_api.py")
    root_conftest._fail_required_skip(report)
    assert report.outcome == "failed"
    assert "EIE_REQUIRE_INTEGRATION=1 forbids skipping integration tests" in str(report.longrepr)
    assert "no Redis at REDIS_URL" in str(report.longrepr)


def test_xfail_report_is_left_alone() -> None:
    report = _skipped_report(
        root_conftest.INTEGRATION_DIR / "test_converge_api.py", wasxfail="known defect"
    )
    root_conftest._fail_required_skip(report)
    assert report.outcome == "skipped"


# ── The CI job that sets the flag ──────────────────────────────────────────────


def _integration_job() -> dict:
    return yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]["integration"]


def test_ci_integration_job_runs_tests_integration_with_the_flag() -> None:
    steps = _integration_job()["steps"]
    run = next(step for step in steps if "pytest tests/integration" in step.get("run", ""))
    assert run["env"][_ENV] == "1"
    assert "-p no:xdist" in run["run"]
    # Every environment condition an integration skip checks is supplied.
    for name in (
        "REDIS_URL",
        "NEO4J_TEST_URI",
        "L9_SEAM_NEO4J_URI",
        "L9_SEAM_NEO4J_PASSWORD",
        "L9_SEAM_GATE_ROOT",
        "L9_SEAM_GATE_PYTHON",
        "L9_SEAM_CEG_ROOT",
        "L9_SEAM_EIE_ROOT",
        "CEG_REPO",
        "CEG_PYTHON",
    ):
        assert name in run["env"], name


def test_ci_integration_job_has_its_services_and_is_advisory() -> None:
    workflow = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    job = workflow["jobs"]["integration"]
    assert set(job["services"]) == {"redis", "neo4j"}
    assert job["services"]["neo4j"]["image"].startswith("neo4j:5.18-enterprise")
    assert job["permissions"] == {"contents": "read"}
    # Advisory: no fan-in gate waits on it.
    assert "integration" not in workflow["jobs"]["ci-gate"]["needs"]
