"""Shared pytest fixtures for all test modules."""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest

from app.models.field_confidence import FieldConfidence, FieldConfidenceMap, FieldSource
from app.models.loop_schemas import ConvergeRequest

REPO_ROOT = Path(__file__).parent.parent


# ── Integration requirement guard ──────────────────────────────────────────────
# CI's `integration` job (ci.yml) sets EIE_REQUIRE_INTEGRATION=1 next to real
# Redis and Neo4j Enterprise services and pinned Constellation.Gate /
# Cognitive.Engine.Graphs checkouts. Under that flag a missing service or peer is
# a broken job, not an optional test: every skip under tests/integration becomes
# a failure, so the job can never report green while having exercised nothing.
# Without the flag (local runs, the `test` jobs) the skip behaviour is unchanged.
# Ported from Cognitive.Engine.Graphs' CEG_REQUIRE_INTEGRATION (CEG #385).

REQUIRE_INTEGRATION_ENV = "EIE_REQUIRE_INTEGRATION"
INTEGRATION_DIR: Path = Path(__file__).parent / "integration"


def integration_required() -> bool:
    """True when EIE_REQUIRE_INTEGRATION is set to 1/true/yes."""
    return os.getenv(REQUIRE_INTEGRATION_ENV, "").strip().lower() in {"1", "true", "yes"}


def skip_or_fail_unavailable(reason: str) -> None:
    """Skip because an integration dependency is unavailable — or fail if required."""
    if integration_required():
        pytest.fail(f"{REQUIRE_INTEGRATION_ENV}=1 but {reason}", pytrace=False)
    pytest.skip(reason)


def _is_integration_path(path: Path) -> bool:
    return path.resolve().is_relative_to(INTEGRATION_DIR.resolve())


def _fail_required_skip(report: pytest.CollectReport | pytest.TestReport) -> None:
    """Rewrite a skip report under tests/integration into a failure (flag on only)."""
    if hasattr(report, "wasxfail"):
        return  # an explicit xfail is a declared expectation, not a missing service
    reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
    report.outcome = "failed"
    report.longrepr = f"{REQUIRE_INTEGRATION_ENV}=1 forbids skipping integration tests: {reason}"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Backstop for skips raised outside skip_or_fail_unavailable (marks, fixtures, bodies)."""
    report = yield
    if report.skipped and integration_required() and _is_integration_path(Path(str(item.path))):
        _fail_required_skip(report)
    return report


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(
    collector: pytest.Collector,
) -> Generator[None, pytest.CollectReport, pytest.CollectReport]:
    """Backstop for module-level skips (e.g. pytest.importorskip) under tests/integration."""
    report = yield
    if (
        report.skipped
        and integration_required()
        and isinstance(collector, pytest.Module)
        and _is_integration_path(Path(str(collector.path)))
    ):
        _fail_required_skip(report)
    return report


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the repository root."""
    return REPO_ROOT


@pytest.fixture(scope="session")
def engine_dir() -> Path:
    """Absolute path to the engine source directory."""
    return REPO_ROOT / "app"


@pytest.fixture
def sample_entity():
    """Standard test entity (plastics recycling)."""
    return {
        "Name": "Acme Recycling Corp",
        "BillingCountry": "US",
        "Industry": "Recycling",
        "polymer_type": "HDPE",
        "contamination_pct": 3.5,
        "facility_tier": "Tier 2",
    }


@pytest.fixture
def sample_schema():
    """Standard target schema."""
    return {
        "polymer_type": "string",
        "contamination_pct": "float",
        "facility_tier": "string",
        "mfi_range": "string",
        "material_grade": "string",
    }


@pytest.fixture
def sample_field_confidence_map():
    """Pre-built FieldConfidenceMap for testing."""
    fcm = FieldConfidenceMap()
    fcm.set(
        FieldConfidence(
            field_name="polymer_type",
            value="HDPE",
            confidence=0.92,
            source=FieldSource.ENRICHMENT,
            variation_agreement=0.80,
            pass_discovered=1,
        )
    )
    fcm.set(
        FieldConfidence(
            field_name="contamination_pct",
            value=3.5,
            confidence=0.68,
            source=FieldSource.ENRICHMENT,
            variation_agreement=0.60,
            pass_discovered=1,
        )
    )
    fcm.set(
        FieldConfidence(
            field_name="material_grade",
            value="A",
            confidence=0.88,
            source=FieldSource.INFERENCE,
            variation_agreement=None,
            pass_discovered=1,
        )
    )
    fcm.set(
        FieldConfidence(
            field_name="facility_tier",
            value="tier_2",
            confidence=0.45,
            source=FieldSource.INFERENCE,
            variation_agreement=None,
            pass_discovered=2,
        )
    )
    fcm.set(
        FieldConfidence(
            field_name="Name",
            value="Acme Plastics",
            confidence=1.0,
            source=FieldSource.CRM,
            variation_agreement=None,
            pass_discovered=1,
        )
    )
    return fcm


@pytest.fixture
def mock_kb_data():
    """Mock domain KB YAML data."""
    return {
        "domain": "plastics_recycling",
        "version": "1.2.0",
        "polymers": {
            "hdpe": {
                "full_name": "High-Density Polyethylene",
                "mfi_range": "0.1-25 g/10min",
                "density": "0.95-0.97 g/cm³",
            },
            "pet": {
                "full_name": "Polyethylene Terephthalate",
                "mfi_range": "10-30 g/10min",
                "density": "1.38-1.39 g/cm³",
            },
        },
        "rules": [
            {
                "name": "premium_hdpe_grade",
                "conditions": {
                    "polymer_type": "HDPE",
                    "contamination_pct": {"lt": 2.0},
                },
                "action": {"set_field": "material_grade", "value": "Premium HDPE"},
                "confidence": 0.95,
            },
        ],
    }


@pytest.fixture
def mock_llm_response():
    """Mock LLM enrichment response."""
    return {
        "confidence": 0.85,
        "polymer_type": "HDPE",
        "mfi_range": "0.5-3.0 g/10min",
        "material_grade": "Standard HDPE",
        "contamination_tolerance_pct": 5.0,
    }


@pytest.fixture
def mock_consensus_payloads():
    """5 validated consensus payloads."""
    return [
        {"confidence": 0.90, "polymer_type": "HDPE", "mfi_range": "0.5-3.0"},
        {"confidence": 0.85, "polymer_type": "HDPE", "mfi_range": "0.5-3.0"},
        {"confidence": 0.88, "polymer_type": "HDPE", "mfi_range": "1.0-4.0"},
        {"confidence": 0.82, "polymer_type": "HDPE", "mfi_range": "0.5-3.0"},
        {"confidence": 0.86, "polymer_type": "HDPE", "mfi_range": "0.5-3.0"},
    ]


@pytest.fixture
def converge_request():
    """Standard ConvergeRequest for loop tests."""
    return ConvergeRequest(
        entity={"Name": "Test Corp"},
        object_type="Account",
        objective="Enrich plastics recycling data",
        domain="plastics_recycling",
        max_passes=5,
        max_budget_tokens=50000,
        convergence_threshold=2.0,
        consensus_threshold=0.65,
        max_variations=5,
    )
