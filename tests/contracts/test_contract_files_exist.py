"""
Contract File Existence Tests
Source: Phase 2 file tree — every generated docs/contracts/ file must exist.
Markers: unit
"""

from __future__ import annotations

import pytest

from tests.contracts.conftest_contracts import CONTRACTS_DIR

EXPECTED_FILES = [
    "README.md",
    "VERSIONING.md",
    "api/README.md",
    "api/openapi.yaml",
    "api/schemas/shared-models.yaml",
    "api/schemas/error-responses.yaml",
    "agents/README.md",
    "agents/tool-schemas/_index.yaml",
    "agents/tool-schemas/enrich-contact.schema.json",
    "agents/tool-schemas/lead-router.schema.json",
    "agents/tool-schemas/deal-risk.schema.json",
    "agents/tool-schemas/data-hygiene.schema.json",
    "agents/tool-schemas/writeback.schema.json",
    "agents/prompt-contracts/_index.yaml",
    "agents/protocols/_index.yaml",
    "agents/protocols/packet-envelope.yaml",
    "data/README.md",
    "data/models/_index.yaml",
    "data/models/enrichment-result.schema.json",
    "data/models/convergence-run.schema.json",
    "data/models/field-confidence-history.schema.json",
    "data/models/schema-proposal.schema.json",
    "data/graph-schema.yaml",
    "data/migrations/migration-policy.md",
    "events/README.md",
    "events/asyncapi.yaml",
    "events/channels/_index.yaml",
    "events/channels/enrichment-events.yaml",
    "events/schemas/event-envelope.yaml",
    "config/README.md",
    "config/env-contract.yaml",
    "dependencies/README.md",
    "dependencies/_index.yaml",
    "dependencies/perplexity-sonar.yaml",
    "dependencies/openai.yaml",
    "dependencies/anthropic.yaml",
    "dependencies/clearbit.yaml",
    "dependencies/zoominfo.yaml",
    "dependencies/apollo.yaml",
    "dependencies/hunter.yaml",
    "dependencies/odoo-crm.yaml",
    "dependencies/salesforce-crm.yaml",
    "dependencies/hubspot-crm.yaml",
    "dependencies/redis.yaml",
    "dependencies/postgresql.yaml",
    "dependencies/constellation-gate.yaml",
    "_templates/api-endpoint.template.yaml",
    "_templates/tool-schema.template.json",
    "_templates/prompt-contract.template.yaml",
    "_templates/event-channel.template.yaml",
    "_templates/data-model.template.json",
]


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", EXPECTED_FILES)
def test_contract_file_exists(rel_path: str) -> None:
    """Every file in the Phase 2 manifest must exist on disk."""
    full_path = CONTRACTS_DIR / rel_path
    assert full_path.exists(), f"Missing contract file: docs/contracts/{rel_path}"


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", EXPECTED_FILES)
def test_contract_file_non_empty(rel_path: str) -> None:
    """Every contract file must be non-empty (> 50 bytes)."""
    full_path = CONTRACTS_DIR / rel_path
    if not full_path.exists():
        pytest.skip(f"Missing: {rel_path}")
    assert full_path.stat().st_size > 50, f"Contract file stub detected: docs/contracts/{rel_path}"
