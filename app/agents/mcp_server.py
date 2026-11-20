"""
Model Context Protocol (MCP) server for the Enrichment Inference Engine.

Exposes enrichment engine capabilities as MCP tools that AI agents
can discover and invoke. This is the primary integration point for
agent-based consumers (including Odoo AI assistants).

L9 Architecture Note:
    This module is the MCP entry point. It delegates to the chassis
    contract (inflate_ingress / deflate_egress) for all engine operations.
    It never directly imports engine internals.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MCP Tool Definitions
# ---------------------------------------------------------------------------


@dataclass
class MCPToolParam:
    """Parameter definition for an MCP tool."""

    name: str
    param_type: str
    description: str
    required: bool = True


@dataclass
class MCPTool:
    """MCP tool definition."""

    name: str
    description: str
    parameters: list[MCPToolParam]


# Tool registry — each tool maps to a chassis handler action
TOOL_REGISTRY: dict[str, MCPTool] = {
    "enrich_contact": MCPTool(
        name="enrich_contact",
        description="Enrich a contact or company record using waterfall enrichment",
        parameters=[
            MCPToolParam("domain", "string", "Domain type: company, contact, or opportunity"),
            MCPToolParam("entity_name", "string", "Name of the entity to enrich"),
            MCPToolParam(
                "entity_data", "object", "Known data fields for the entity", required=False
            ),
            MCPToolParam(
                "quality_threshold", "number", "Minimum quality score (0-1)", required=False
            ),
        ],
    ),
    "lead_router": MCPTool(
        name="lead_router",
        description="Route a lead to the best sales rep based on enrichment scores",
        parameters=[
            MCPToolParam("lead_data", "object", "Lead record data"),
            MCPToolParam(
                "team_config", "object", "Sales team routing configuration", required=False
            ),
        ],
    ),
    "deal_risk": MCPTool(
        name="deal_risk",
        description="Assess risk score for a deal/opportunity based on enrichment data",
        parameters=[
            MCPToolParam("deal_data", "object", "Deal/opportunity record data"),
            MCPToolParam("historical_context", "object", "Historical deal context", required=False),
        ],
    ),
    "data_hygiene": MCPTool(
        name="data_hygiene",
        description="Assess data quality and recommend hygiene actions for CRM records",
        parameters=[
            MCPToolParam("records", "array", "List of CRM records to assess"),
            MCPToolParam("domain", "string", "Domain type: company, contact, or opportunity"),
        ],
    ),
    "writeback": MCPTool(
        name="writeback",
        description="Write enriched data back to the CRM system",
        parameters=[
            MCPToolParam(
                "crm_type",
                "string",
                "CRM provider key, as registered by the trusted connector owner",
            ),
            MCPToolParam("object_type", "string", "CRM object type to write to"),
            MCPToolParam("record_id", "string", "Target record ID"),
            MCPToolParam("enriched_data", "object", "Enriched data to write back"),
            MCPToolParam(
                "confidence_threshold", "number", "Min confidence for writeback", required=False
            ),
        ],
    ),
}


# ---------------------------------------------------------------------------
# MCP Resource Definitions
# ---------------------------------------------------------------------------


@dataclass
class MCPResource:
    """MCP resource definition."""

    uri: str
    name: str
    description: str
    mime_type: str = "application/json"


RESOURCE_REGISTRY: dict[str, MCPResource] = {
    "crm://contacts": MCPResource(
        uri="crm://contacts",
        name="CRM Contacts",
        description="Read/write access to CRM contact records",
    ),
    "crm://companies": MCPResource(
        uri="crm://companies",
        name="CRM Companies",
        description="Read/write access to CRM company records",
    ),
    "pipeline://forecast": MCPResource(
        uri="pipeline://forecast",
        name="Pipeline Forecast",
        description="Real-time pipeline forecast with AI scoring",
    ),
    "enrichment://status": MCPResource(
        uri="enrichment://status",
        name="Enrichment Status",
        description="Current enrichment queue status and metrics",
    ),
}


# ---------------------------------------------------------------------------
# MCP Server Protocol Handler
# ---------------------------------------------------------------------------


class MCPServer:
    """
    MCP protocol handler for the Enrichment Inference Engine.

    Handles the MCP lifecycle:
    1. tools/list — returns available tools
    2. tools/call — dispatches to chassis handler
    3. resources/list — returns available resources
    4. resources/read — fetches resource data
    """

    def __init__(self, chassis_dispatch: Any = None) -> None:
        """
        Initialize MCP server.

        Args:
            chassis_dispatch: Callable that accepts (action, tenant, payload)
                and returns a result dict. This is the chassis handler bridge.
        """
        self._dispatch = chassis_dispatch

    def handle_request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Route an MCP request to the appropriate handler."""
        params = params or {}

        handlers = {
            "tools/list": self._list_tools,
            "tools/call": self._call_tool,
            "resources/list": self._list_resources,
            "resources/read": self._read_resource,
        }

        handler = handlers.get(method)
        if not handler:
            return {"error": {"code": -32601, "message": f"Unknown method: {method}"}}

        try:
            return handler(params)
        except Exception as exc:
            logger.exception("MCP handler error: %s", exc)
            return {"error": {"code": -32603, "message": str(exc)}}

    def _list_tools(self, _params: dict[str, Any]) -> dict[str, Any]:
        """Return the list of available MCP tools."""
        tools = []
        for tool in TOOL_REGISTRY.values():
            properties = {}
            required = []
            for p in tool.parameters:
                properties[p.name] = {
                    "type": p.param_type,
                    "description": p.description,
                }
                if p.required:
                    required.append(p.name)

            tools.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                }
            )

        return {"tools": tools}

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a tool call to the chassis handler."""
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name not in TOOL_REGISTRY:
            return {
                "error": {
                    "code": -32602,
                    "message": f"Unknown tool: {tool_name}",
                }
            }

        if not isinstance(arguments, dict):
            return {
                "error": {
                    "code": -32602,
                    "message": "Tool arguments must be an object",
                }
            }

        # U00 found no runtime binding by which this compatibility MCP server can
        # establish a signed tenant, trusted connector, record/field-bound human
        # approval, or durable CRM write journal. A caller-provided `tenant` (or
        # any other mutation field) is not authority, so writeback must stop here
        # before arbitrary chassis dispatch and provider construction.
        if tool_name == "writeback":
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Error: writeback is disabled outside a signed Gate runtime "
                            "with trusted connector, approval, and durable journal context"
                        ),
                    }
                ],
                "isError": True,
            }

        if not self._dispatch:
            return {
                "error": {
                    "code": -32603,
                    "message": "Chassis dispatch not configured",
                }
            }

        # Map MCP tool names to chassis handler actions
        action_map = {
            "enrich_contact": "enrich",
            "lead_router": "enrich",
            "deal_risk": "enrich",
            "data_hygiene": "enrich",
            "writeback": "writeback",
        }

        action = action_map.get(tool_name, "enrich")
        call_arguments = dict(arguments)
        tenant = call_arguments.pop("tenant", "default")

        try:
            result = self._dispatch(action, tenant, call_arguments)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, default=str),
                    }
                ]
            }
        except Exception as exc:
            return {
                "content": [{"type": "text", "text": f"Error: {exc}"}],
                "isError": True,
            }

    def _list_resources(self, _params: dict[str, Any]) -> dict[str, Any]:
        """Return the list of available MCP resources."""
        return {"resources": [asdict(r) for r in RESOURCE_REGISTRY.values()]}

    def _read_resource(self, params: dict[str, Any]) -> dict[str, Any]:
        """Read a specific MCP resource."""
        uri = params.get("uri", "")
        resource = RESOURCE_REGISTRY.get(uri)
        if not resource:
            return {
                "error": {
                    "code": -32602,
                    "message": f"Unknown resource: {uri}",
                }
            }

        # Resource data would be fetched from the engine at runtime
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": resource.mime_type,
                    "text": json.dumps({"status": "available", "uri": uri}),
                }
            ]
        }
