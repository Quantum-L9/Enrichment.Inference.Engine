"""Tier 2 — transport tenant is authoritative (TA-001..TA-003).

This module is collection-safe for `pytest tests/contracts/ -m "unit and not
enforcement"`. It must not import app.engines.handlers (that stack requires
the perplexity extra, which the constitution job does not install).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.enforcement]

ROOT = Path(".")
OPENAPI_PATH = ROOT / "docs/contracts/api/openapi.yaml"
MIGRATION_PATH = ROOT / "migrations/versions/001_initial_schema.py"
CONTRACT_PATH = ROOT / "docs/contracts/enforcement/tenant-authority.yaml"
HANDLERS_PATH = ROOT / "app/engines/handlers.py"
CHASSIS_PATH = ROOT / "app/services/chassis_handlers.py"


def test_tenant_authority_contract_declares_three_rules() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["id"] == "tenant-authority"
    assert {rule["id"] for rule in contract["rules"]} == {"TA-001", "TA-002", "TA-003"}


def test_handlers_reject_payload_tenant_override() -> None:
    for path in (HANDLERS_PATH, CHASSIS_PATH):
        source = path.read_text(encoding="utf-8")
        assert "tenant_mismatch" in source
        assert "def _authoritative_tenant" in source


def test_approval_request_contract_requires_tenant_id() -> None:
    openapi = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    schema = openapi["components"]["schemas"]["ApprovalRequest"]
    assert "tenant_id" in schema["required"]
    assert schema["properties"]["tenant_id"]["type"] == "string"


def test_initial_schema_uses_tenant_scoped_idempotency() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "uq_enrichment_results_tenant_idempotency" in source
    assert "unique=True" not in source
