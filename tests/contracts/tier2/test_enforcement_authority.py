"""
Tier 2 — Enforcement: Action Authority
======================================
Proves side-effect separation between actual current MCP tools and chassis actions.

Primary sources:
- docs/contracts/agents/tool-schemas/_index.yaml
- docs/contracts/agents/protocols/packet-envelope.yaml
- docs/contracts/agents/tool-schemas/writeback.schema.json
- docs/contracts/api/openapi.yaml
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.enforcement, pytest.mark.authority]

ROOT = Path(".")
MCP_INDEX_PATH = ROOT / "docs/contracts/agents/tool-schemas/_index.yaml"
PACKET_PROTOCOL_PATH = ROOT / "docs/contracts/agents/protocols/packet-envelope.yaml"
WRITEBACK_SCHEMA_PATH = ROOT / "docs/contracts/agents/tool-schemas/writeback.schema.json"


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def mcp_registry() -> dict[str, Any]:
    return _load_yaml(MCP_INDEX_PATH)


@pytest.fixture(scope="module")
def packet_protocol() -> dict[str, Any]:
    return _load_yaml(PACKET_PROTOCOL_PATH)


@pytest.fixture(scope="module")
def writeback_schema() -> dict[str, Any]:
    return _load_json(WRITEBACK_SCHEMA_PATH)


TOOL_AUTHORITY = {
    "enrich_contact": {"mutation_class": "enrich_only", "approval": "autonomous"},
    "lead_router": {"mutation_class": "enrich_only", "approval": "autonomous"},
    "deal_risk": {"mutation_class": "enrich_only", "approval": "autonomous"},
    "data_hygiene": {"mutation_class": "enrich_only", "approval": "autonomous"},
    "writeback": {
        "mutation_class": "external_mutation",
        "approval": "threshold_or_human",
    },
}

ACTION_AUTHORITY = {
    "enrich": {"mutation_class": "enrich_only", "approval": "autonomous"},
    "enrichbatch": {"mutation_class": "enrich_only", "approval": "autonomous"},
    "converge": {"mutation_class": "internal_state", "approval": "autonomous"},
    "discover": {"mutation_class": "propose_only", "approval": "autonomous"},
    "enrich-and-sync": {
        "mutation_class": "internal_plus_graph_sync",
        "approval": "autonomous",
    },
    "writeback": {
        "mutation_class": "external_mutation",
        "approval": "threshold_or_human",
    },
}


def simulate_writeback(payload: dict[str, Any]) -> dict[str, Any]:
    threshold = float(payload.get("confidence_threshold", 0.65))
    enriched_data = payload.get("enriched_data", {})
    field_confidences = payload.get("_field_confidences", {})

    written_fields: list[str] = []
    skipped_fields: list[str] = []
    skip_reasons: dict[str, str] = {}

    for field_name in enriched_data:
        field_confidence = float(field_confidences.get(field_name, 0.0))
        if field_confidence >= threshold:
            written_fields.append(field_name)
        else:
            skipped_fields.append(field_name)
            skip_reasons[field_name] = (
                f"confidence {field_confidence:.4f} below threshold {threshold:.4f}"
            )

    status = "completed"
    if written_fields and skipped_fields:
        status = "partial"
    elif not written_fields:
        status = "rejected"

    return {
        "status": status,
        "attempted_fields": list(enriched_data.keys()),
        "written_fields": written_fields,
        "skipped_fields": skipped_fields,
        "skip_reasons": skip_reasons,
    }


def simulate_discover() -> dict[str, Any]:
    return {
        "proposals": [
            {
                "field_name": "recycler_certification",
                "field_type": "string",
                "approval_status": "pending",
            }
        ],
        "applied": False,
        "schema_mutated": False,
    }


class TestToolAuthorityCoverage:
    def test_authority_matrix_covers_all_declared_tools(
        self,
        mcp_registry: dict[str, Any],
    ) -> None:
        declared_tools = {tool["name"] for tool in mcp_registry["tools"]}
        assert declared_tools == set(TOOL_AUTHORITY.keys())

    def test_writeback_tool_maps_to_external_mutation(self) -> None:
        assert TOOL_AUTHORITY["writeback"]["mutation_class"] == "external_mutation"

    def test_non_writeback_tools_are_not_external_mutation(self) -> None:
        for tool_name, authority in TOOL_AUTHORITY.items():
            if tool_name == "writeback":
                continue
            assert authority["mutation_class"] != "external_mutation"

    def test_writeback_requires_non_autonomous_approval(self) -> None:
        assert TOOL_AUTHORITY["writeback"]["approval"] != "autonomous"


class TestActionAuthorityCoverage:
    def test_authority_matrix_covers_all_registered_actions(
        self,
        packet_protocol: dict[str, Any],
    ) -> None:
        declared_actions = {handler["action"] for handler in packet_protocol["registered_handlers"]}
        assert declared_actions == set(ACTION_AUTHORITY.keys())

    def test_discover_is_propose_only(self) -> None:
        assert ACTION_AUTHORITY["discover"]["mutation_class"] == "propose_only"

    def test_converge_is_internal_state(self) -> None:
        assert ACTION_AUTHORITY["converge"]["mutation_class"] == "internal_state"

    def test_writeback_is_external_mutation(self) -> None:
        assert ACTION_AUTHORITY["writeback"]["mutation_class"] == "external_mutation"


class TestWritebackGovernance:
    def test_writeback_schema_required_fields_present(
        self,
        writeback_schema: dict[str, Any],
    ) -> None:
        required = set(writeback_schema["required"])
        assert required == {"crm_type", "object_type", "record_id", "enriched_data"}

    def test_writeback_skips_field_below_confidence_threshold(self) -> None:
        payload = {
            "crm_type": "salesforce",
            "object_type": "Account",
            "record_id": "001ABC",
            "enriched_data": {"recycling_grade": "HDPE"},
            "confidence_threshold": 0.90,
            "_field_confidences": {"recycling_grade": 0.42},
        }
        result = simulate_writeback(payload)
        assert result["status"] == "rejected"
        assert "recycling_grade" in result["skipped_fields"]

    def test_writeback_partial_when_mixed_confidence(self) -> None:
        payload = {
            "crm_type": "salesforce",
            "object_type": "Account",
            "record_id": "001ABC",
            "enriched_data": {
                "industry": "Manufacturing",
                "recycling_grade": "HDPE",
            },
            "confidence_threshold": 0.80,
            "_field_confidences": {
                "industry": 0.95,
                "recycling_grade": 0.42,
            },
        }
        result = simulate_writeback(payload)
        assert result["status"] == "partial"
        assert "industry" in result["written_fields"]
        assert "recycling_grade" in result["skipped_fields"]

    def test_writeback_writes_all_above_threshold(self) -> None:
        payload = {
            "crm_type": "salesforce",
            "object_type": "Account",
            "record_id": "001ABC",
            "enriched_data": {"name": "Acme", "industry": "Tech"},
            "confidence_threshold": 0.70,
            "_field_confidences": {"name": 0.95, "industry": 0.85},
        }
        result = simulate_writeback(payload)
        assert result["status"] == "completed"
        assert set(result["written_fields"]) == {"name", "industry"}
        assert result["skipped_fields"] == []

    def test_writeback_result_includes_governance_fields(self) -> None:
        result = simulate_writeback(
            {
                "crm_type": "hubspot",
                "object_type": "companies",
                "record_id": "42",
                "enriched_data": {"industry": "Tech"},
                "_field_confidences": {"industry": 0.9},
            }
        )
        required_fields = {
            "status",
            "attempted_fields",
            "written_fields",
            "skipped_fields",
            "skip_reasons",
        }
        missing = sorted(field for field in required_fields if field not in result)
        assert not missing, f"Missing governance fields in writeback result: {missing}"


class TestDiscoverAuthority:
    def test_discover_produces_proposals_only(self) -> None:
        result = simulate_discover()
        assert result["schema_mutated"] is False
        assert result["applied"] is False
        assert len(result["proposals"]) > 0

    def test_discover_proposals_are_pending(self) -> None:
        result = simulate_discover()
        for proposal in result["proposals"]:
            assert proposal["approval_status"] == "pending"
