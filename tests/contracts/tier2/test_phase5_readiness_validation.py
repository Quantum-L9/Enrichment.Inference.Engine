from __future__ import annotations

import pytest

from scripts.validate_phase5_readiness import run_phase5_readiness

pytestmark = [pytest.mark.unit, pytest.mark.enforcement]

# The runtime attestation counts a dependency ready only when every env var its
# dependency contract declares is set (app/services/runtime_attestation.py).
# runtime_truth authorizes `writeback` with crm_type=salesforce, which requires
# Redis (docs/contracts/dependencies/redis.yaml) and the Salesforce CRM target
# (salesforce-crm.yaml). These placeholders make the attested environment
# genuinely ready instead of depending on the shell that runs pytest; no
# service is contacted.
_READY_ENVIRONMENT = {
    "REDIS_URL": "redis://phase5-fixture.invalid:6379/0",
    "SALESFORCE_CLIENT_ID": "phase5-fixture",
    "SALESFORCE_CLIENT_SECRET": "phase5-fixture",
    "SALESFORCE_USERNAME": "phase5-fixture",
    "SALESFORCE_PASSWORD": "phase5-fixture",
    "SALESFORCE_SECURITY_TOKEN": "phase5-fixture",
}


def test_phase5_readiness_checks_pass_without_subprocess_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in _READY_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    report = run_phase5_readiness(run_pytest=False)
    assert report["overall_ready_for_phase5"] is True, report


def test_phase5_readiness_reports_all_five_truths() -> None:
    report = run_phase5_readiness(run_pytest=False)
    truth_names = {item["name"] for item in report["results"]}
    assert truth_names == {
        "repo_truth",
        "runtime_truth",
        "governance_truth",
        "operational_truth",
        "agent_truth",
    }
