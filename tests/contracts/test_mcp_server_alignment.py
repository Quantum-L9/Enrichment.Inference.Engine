"""
MCP Server Live Alignment Tests
================================
These tests import app/agents/mcp_server.py directly and verify that the
TOOL_REGISTRY and RESOURCE_REGISTRY match the documented contracts.

If the MCP server changes (new tools added, params renamed), these tests
will catch the contract drift immediately in CI.

Source: app/agents/mcp_server.py TOOL_REGISTRY, RESOURCE_REGISTRY, MCPServer
Markers: unit (pure Python, no network)
"""

from __future__ import annotations

import pytest

from tests.contracts.conftest_contracts import AGENTS_DIR, load_json

# ---------------------------------------------------------------------------
# Import MCP server — fail early if not importable
# ---------------------------------------------------------------------------

try:
    from app.agents.mcp_server import (
        RESOURCE_REGISTRY,
        TOOL_REGISTRY,
        MCPServer,
    )

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

mcp_required = pytest.mark.skipif(
    not MCP_AVAILABLE,
    reason="app package not on PYTHONPATH — run from repo root",
)


# ---------------------------------------------------------------------------
# TOOL_REGISTRY live checks
# ---------------------------------------------------------------------------

CONTRACTED_TOOLS = ("data_hygiene", "deal_risk", "enrich_contact", "lead_router", "writeback")


@mcp_required
@pytest.mark.unit
def test_tool_registry_contains_all_contracted_tools() -> None:
    """Live TOOL_REGISTRY must contain all 5 contracted tools."""
    live_tools = set(TOOL_REGISTRY.keys())
    missing = set(CONTRACTED_TOOLS) - live_tools
    assert not missing, (
        f"TOOL_REGISTRY missing contracted tools: {missing}.\n"
        "Contract: docs/contracts/agents/tool-schemas/_index.yaml\n"
        "Source:   app/agents/mcp_server.py TOOL_REGISTRY"
    )


@mcp_required
@pytest.mark.unit
def test_no_undocumented_tools_in_registry() -> None:
    """
    Every tool in live TOOL_REGISTRY must have a corresponding contract schema file.
    New tools added to TOOL_REGISTRY without a contract = contract drift.
    """
    live_tools = set(TOOL_REGISTRY.keys())
    undocumented = []
    for tool_name in live_tools:
        # Contract file name: kebab-case of tool_name
        kebab_name = tool_name.replace("_", "-")
        schema_path = AGENTS_DIR / "tool-schemas" / f"{kebab_name}.schema.json"
        if not schema_path.exists():
            undocumented.append(tool_name)

    assert not undocumented, (
        f"Undocumented tools in TOOL_REGISTRY: {undocumented}.\n"
        "Create corresponding tool-schema files for each undocumented tool."
    )


@mcp_required
@pytest.mark.unit
@pytest.mark.parametrize("tool_name", CONTRACTED_TOOLS)
def test_tool_parameters_match_contract(tool_name: str) -> None:
    """
    Live MCPTool parameters must match documented parameters in contract schema.
    """
    if tool_name not in TOOL_REGISTRY:
        pytest.skip(f"{tool_name} not in live TOOL_REGISTRY")

    kebab_name = tool_name.replace("_", "-")
    schema_path = AGENTS_DIR / "tool-schemas" / f"{kebab_name}.schema.json"
    if not schema_path.exists():
        pytest.skip(f"Contract schema missing for {tool_name}")

    schema = load_json(schema_path)
    contract_props = set(schema.get("properties", {}).keys())

    live_tool = TOOL_REGISTRY[tool_name]
    live_params = {p.name for p in live_tool.parameters}

    # Every live param must be in the contract
    undocumented_params = live_params - contract_props
    assert not undocumented_params, (
        f"{tool_name}: live params not in contract: {undocumented_params}.\n"
        "Contract drift detected — update the tool schema."
    )


# ---------------------------------------------------------------------------
# MCPServer protocol handler tests
# ---------------------------------------------------------------------------


@mcp_required
@pytest.mark.unit
def test_mcp_server_tools_list_returns_all_tools() -> None:
    """tools/list must return all tools in TOOL_REGISTRY."""
    server = MCPServer()
    result = server.handle_request("tools/list")
    assert "tools" in result, "tools/list response missing 'tools' key"
    returned_names = {t["name"] for t in result["tools"]}
    assert returned_names == set(TOOL_REGISTRY.keys()), (
        f"tools/list returned {returned_names}, expected {set(TOOL_REGISTRY.keys())}"
    )


@mcp_required
@pytest.mark.unit
def test_mcp_server_unknown_method_returns_error() -> None:
    """Unknown MCP method must return error code -32601."""
    server = MCPServer()
    result = server.handle_request("nonexistent/method")
    assert "error" in result
    assert result["error"]["code"] == -32601


@mcp_required
@pytest.mark.unit
def test_mcp_server_unknown_tool_call_returns_error() -> None:
    """Calling an unknown tool must return error code -32602."""
    server = MCPServer()
    result = server.handle_request("tools/call", {"name": "nonexistent_tool", "arguments": {}})
    assert "error" in result
    assert result["error"]["code"] == -32602


@mcp_required
@pytest.mark.unit
def test_mcp_server_resources_list_returns_all_resources() -> None:
    """resources/list must return all resources in RESOURCE_REGISTRY."""
    server = MCPServer()
    result = server.handle_request("resources/list")
    assert "resources" in result
    returned_uris = {r["uri"] for r in result["resources"]}
    expected_uris = set(RESOURCE_REGISTRY.keys())
    assert returned_uris == expected_uris, (
        f"resources/list returned {returned_uris}, expected {expected_uris}"
    )


@mcp_required
@pytest.mark.unit
def test_mcp_server_tools_list_has_input_schemas() -> None:
    """Every tool in tools/list response must have inputSchema."""
    server = MCPServer()
    result = server.handle_request("tools/list")
    for tool in result.get("tools", []):
        assert "inputSchema" in tool, (
            f"Tool '{tool.get('name')}' missing inputSchema in tools/list response"
        )
        schema = tool["inputSchema"]
        assert schema.get("type") == "object"
        assert "properties" in schema
        assert "required" in schema


@mcp_required
@pytest.mark.unit
def test_mcp_server_no_dispatch_returns_error_not_exception() -> None:
    """
    Calling a tool without chassis_dispatch configured must return structured error,
    not raise an exception.
    """
    server = MCPServer(chassis_dispatch=None)
    result = server.handle_request(
        "tools/call",
        {"name": "enrich_contact", "arguments": {"domain": "company", "entity_name": "Acme"}},
    )
    assert "error" in result, (
        "tools/call without dispatch must return error, not raise. "
        "Source: app/agents/mcp_server.py _call_tool"
    )


# ---------------------------------------------------------------------------
# Contract-to-live description alignment
# ---------------------------------------------------------------------------


@mcp_required
@pytest.mark.unit
@pytest.mark.parametrize("tool_name", CONTRACTED_TOOLS)
def test_tool_description_not_empty_in_live_registry(tool_name: str) -> None:
    """Live tool descriptions must be non-empty."""
    if tool_name not in TOOL_REGISTRY:
        pytest.skip(f"{tool_name} not in TOOL_REGISTRY")
    live_desc = TOOL_REGISTRY[tool_name].description
    assert live_desc, f"{tool_name}: TOOL_REGISTRY description is empty"
    assert len(live_desc) > 10, f"{tool_name}: TOOL_REGISTRY description is too short"


@mcp_required
@pytest.mark.unit
@pytest.mark.parametrize("tool_name", CONTRACTED_TOOLS)
def test_tool_required_params_not_empty(tool_name: str) -> None:
    """Each tool must have at least one required parameter."""
    if tool_name not in TOOL_REGISTRY:
        pytest.skip(f"{tool_name} not in TOOL_REGISTRY")
    tool = TOOL_REGISTRY[tool_name]
    required_params = [p for p in tool.parameters if p.required]
    assert required_params, (
        f"{tool_name}: no required parameters defined in TOOL_REGISTRY. "
        "Every tool must require at least one parameter."
    )
