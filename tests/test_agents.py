"""
Tests for agent modules.

Validates MCP server, lead router, deal risk, data hygiene, and handoff agents.
"""

from __future__ import annotations

import json

from app.agents.data_hygiene import DataHygieneAgent
from app.agents.deal_risk import DealRiskAgent
from app.agents.handoff import HandoffAgent
from app.agents.lead_router import LeadRouterAgent
from app.agents.mcp_server import MCPServer

# ---------------------------------------------------------------------------
# MCP Server Tests
# ---------------------------------------------------------------------------


class TestMCPServer:
    """MCP protocol handler tests."""

    def test_list_tools(self) -> None:
        server = MCPServer()
        result = server.handle_request("tools/list")
        assert "tools" in result
        tool_names = {t["name"] for t in result["tools"]}
        assert "enrich_contact" in tool_names
        assert "writeback" in tool_names
        assert "lead_router" in tool_names
        assert "deal_risk" in tool_names
        assert "data_hygiene" in tool_names

    def test_list_resources(self) -> None:
        server = MCPServer()
        result = server.handle_request("resources/list")
        assert "resources" in result
        uris = {r["uri"] for r in result["resources"]}
        assert "crm://contacts" in uris
        assert "enrichment://status" in uris

    def test_unknown_method(self) -> None:
        server = MCPServer()
        result = server.handle_request("unknown/method")
        assert "error" in result
        assert result["error"]["code"] == -32601

    def test_call_unknown_tool(self) -> None:
        server = MCPServer()
        result = server.handle_request("tools/call", {"name": "nonexistent"})
        assert "error" in result
        assert result["error"]["code"] == -32602

    def test_call_tool_without_dispatch(self) -> None:
        server = MCPServer()
        result = server.handle_request("tools/call", {"name": "enrich_contact"})
        assert "error" in result
        assert "dispatch" in result["error"]["message"].lower()

    def test_call_tool_with_dispatch(self) -> None:
        def mock_dispatch(action: str, tenant: str, payload: dict) -> dict:
            return {"action": action, "tenant": tenant, "status": "ok"}

        server = MCPServer(chassis_dispatch=mock_dispatch)
        result = server.handle_request(
            "tools/call",
            {
                "name": "enrich_contact",
                "arguments": {"domain": "company", "entity_name": "Test"},
            },
        )
        assert "content" in result
        data = json.loads(result["content"][0]["text"])
        assert data["action"] == "enrich"
        assert data["status"] == "ok"

    def test_read_known_resource(self) -> None:
        server = MCPServer()
        result = server.handle_request("resources/read", {"uri": "crm://contacts"})
        assert "contents" in result
        assert result["contents"][0]["uri"] == "crm://contacts"

    def test_read_unknown_resource(self) -> None:
        server = MCPServer()
        result = server.handle_request("resources/read", {"uri": "unknown://x"})
        assert "error" in result

    def test_tool_schemas_have_required_fields(self) -> None:
        server = MCPServer()
        result = server.handle_request("tools/list")
        for tool in result["tools"]:
            schema = tool["inputSchema"]
            assert schema["type"] == "object"
            assert "properties" in schema
            assert "required" in schema


# ---------------------------------------------------------------------------
# Lead Router Tests
# ---------------------------------------------------------------------------


class TestLeadRouter:
    """Lead routing agent tests."""

    def test_no_reps_returns_unassigned(self) -> None:
        agent = LeadRouterAgent()
        result = agent.route({"company_name": "Test"})
        assert result.assigned_rep == "unassigned"
        assert result.confidence == 0.0

    def test_territory_match(self) -> None:
        config = {
            "reps": [
                {"name": "Alice", "territories": ["US"], "industries": []},
                {"name": "Bob", "territories": ["UK"], "industries": []},
            ]
        }
        agent = LeadRouterAgent(team_config=config)
        result = agent.route({"company_location_country": "US"})
        assert result.assigned_rep == "Alice"
        assert result.confidence > 0

    def test_industry_match(self) -> None:
        config = {
            "reps": [
                {"name": "Alice", "territories": [], "industries": ["SaaS"]},
                {"name": "Bob", "territories": [], "industries": ["Finance"]},
            ]
        }
        agent = LeadRouterAgent(team_config=config)
        result = agent.route({"company_industry": "SaaS"})
        assert result.assigned_rep == "Alice"

    def test_fallback_reps_populated(self) -> None:
        config = {
            "reps": [
                {"name": "Alice", "territories": ["US"], "industries": []},
                {"name": "Bob", "territories": [], "industries": []},
                {"name": "Carol", "territories": [], "industries": []},
            ]
        }
        agent = LeadRouterAgent(team_config=config)
        result = agent.route({"company_location_country": "US"})
        assert len(result.fallback_reps) <= 2


# ---------------------------------------------------------------------------
# Deal Risk Tests
# ---------------------------------------------------------------------------


class TestDealRisk:
    """Deal risk assessment agent tests."""

    def test_complete_deal_low_risk(self) -> None:
        agent = DealRiskAgent()
        result = agent.assess(
            {
                "opportunity_name": "Big Deal",
                "opportunity_stage": "negotiation",
                "opportunity_amount": 100000,
                "opportunity_close_date": "2026-06-01",
                "opportunity_account_id": "acc-123",
                "opportunity_stakeholders": [{"name": "CEO", "role": "decision_maker"}],
            },
            enrichment_quality=0.9,
        )
        assert result.risk_level in ("low", "medium")
        assert result.risk_score < 0.5

    def test_missing_fields_high_risk(self) -> None:
        agent = DealRiskAgent()
        result = agent.assess({}, enrichment_quality=0.1)
        assert result.risk_level in ("high", "critical")
        assert result.risk_score > 0.5

    def test_dimension_scores_present(self) -> None:
        agent = DealRiskAgent()
        result = agent.assess({"opportunity_name": "Test"})
        assert "data_completeness" in result.dimension_scores
        assert "enrichment_quality" in result.dimension_scores
        assert "timeline" in result.dimension_scores
        assert "stakeholder_coverage" in result.dimension_scores
        assert "competitive_pressure" in result.dimension_scores


# ---------------------------------------------------------------------------
# Data Hygiene Tests
# ---------------------------------------------------------------------------


class TestDataHygiene:
    """Data hygiene agent tests."""

    def test_empty_records(self) -> None:
        agent = DataHygieneAgent()
        report = agent.assess([], domain="company")
        assert report.total_records == 0
        assert report.quality_score == 0.0

    def test_complete_records_high_quality(self) -> None:
        agent = DataHygieneAgent()
        records = [
            {
                "id": "1",
                "company_name": "Test Corp",
                "company_domain": "test.com",
                "company_industry": "SaaS",
                "employee_count": 100,
                "company_location_country": "US",
                "last_enriched_at": "2026-01-01",
            }
        ]
        report = agent.assess(records, domain="company")
        assert report.quality_score == 1.0

    def test_incomplete_records_generate_actions(self) -> None:
        agent = DataHygieneAgent()
        records = [{"id": "1", "company_name": "Test"}]
        report = agent.assess(records, domain="company")
        assert report.quality_score < 1.0
        assert len(report.actions) > 0
        assert any(a.action_type == "enrich" for a in report.actions)

    def test_partial_records_preserve_fractional_quality(self) -> None:
        agent = DataHygieneAgent()
        report = agent.assess(
            [
                {
                    "id": "1",
                    "company_name": "Test",
                    "company_domain": "test.example",
                }
            ],
            domain="company",
        )
        assert report.quality_score == 0.4

    def test_invalid_email_generates_validate_action(self) -> None:
        agent = DataHygieneAgent()
        records = [{"id": "1", "contact_email": "not-an-email"}]
        report = agent.assess(records, domain="contact")
        assert any(a.action_type == "validate" for a in report.actions)


# ---------------------------------------------------------------------------
# Handoff Tests
# ---------------------------------------------------------------------------


class TestHandoff:
    """Sales-to-CS handoff agent tests."""

    def test_basic_handoff(self) -> None:
        agent = HandoffAgent()
        doc = agent.generate(
            deal_data={
                "opportunity_id": "opp-123",
                "account_name": "Test Corp",
                "opportunity_name": "Big Deal",
                "opportunity_amount": 100000,
                "opportunity_stage": "closed_won",
            },
            enrichment_data={
                "company_name": "Test Corp",
                "company_industry": "SaaS",
                "company_location_city": "Austin",
                "company_location_state": "TX",
                "company_location_country": "US",
            },
        )
        assert doc.deal_id == "opp-123"
        assert doc.account_name == "Test Corp"
        assert "account_overview" in doc.sections
        assert "deal_summary" in doc.sections
        assert len(doc.action_items) > 0

    def test_missing_stakeholders_flagged(self) -> None:
        agent = HandoffAgent()
        doc = agent.generate(deal_data={"opportunity_id": "opp-1"})
        assert any("stakeholder" in f.lower() for f in doc.risk_flags)

    def test_location_builder(self) -> None:
        agent = HandoffAgent()
        loc = agent._build_location(
            {
                "company_location_city": "Austin",
                "company_location_state": "TX",
                "company_location_country": "US",
            }
        )
        assert "Austin" in loc
        assert "TX" in loc
        assert "US" in loc
