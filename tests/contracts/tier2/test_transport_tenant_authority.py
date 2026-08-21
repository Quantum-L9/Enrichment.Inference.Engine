"""Tier 2 — transport tenant is authoritative (TA-001..TA-003)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.engines.handlers import _authoritative_tenant
from app.services.chassis_handlers import (
    _authoritative_tenant as chassis_authoritative_tenant,
)

pytestmark = [pytest.mark.unit, pytest.mark.enforcement]

ROOT = Path(".")
OPENAPI_PATH = ROOT / "docs/contracts/api/openapi.yaml"
MIGRATION_PATH = ROOT / "migrations/versions/001_initial_schema.py"
CONTRACT_PATH = ROOT / "docs/contracts/enforcement/tenant-authority.yaml"


def test_tenant_authority_contract_declares_three_rules() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["id"] == "tenant-authority"
    assert {rule["id"] for rule in contract["rules"]} == {"TA-001", "TA-002", "TA-003"}


@pytest.mark.parametrize(
    "resolver",
    [_authoritative_tenant, chassis_authoritative_tenant],
)
def test_payload_tenant_cannot_override_transport(resolver) -> None:
    rejected = resolver("transport", {"tenant_id": "payload"})
    assert rejected == {
        "status": "rejected",
        "error": "tenant_mismatch",
        "tenant_id": "transport",
    }
    assert resolver("acme", {"tenant_id": "acme"}) == "acme"
    assert resolver("acme", {}) == "acme"


def test_approval_request_contract_requires_tenant_id() -> None:
    openapi = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    schema = openapi["components"]["schemas"]["ApprovalRequest"]
    assert "tenant_id" in schema["required"]
    assert schema["properties"]["tenant_id"]["type"] == "string"


def test_initial_schema_uses_tenant_scoped_idempotency() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "uq_enrichment_results_tenant_idempotency" in source
    assert "unique=True" not in source
