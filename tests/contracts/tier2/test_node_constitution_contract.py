from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.enforcement]

ROOT = Path(".")
CONSTITUTION_PATH = ROOT / "docs/contracts/node.constitution.yaml"
PACKET_PROTOCOL_PATH = ROOT / "docs/contracts/agents/protocols/packet-envelope.yaml"
MCP_INDEX_PATH = ROOT / "docs/contracts/agents/tool-schemas/_index.yaml"
ASYNCAPI_PATH = ROOT / "docs/contracts/events/asyncapi.yaml"
DEPENDENCY_INDEX_PATH = ROOT / "docs/contracts/dependencies/_index.yaml"
ATTESTATION_SCHEMA_PATH = ROOT / "docs/contracts/runtime-attestation.schema.json"


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def constitution() -> dict[str, Any]:
    return _load_yaml(CONSTITUTION_PATH)


@pytest.fixture(scope="module")
def packet_protocol() -> dict[str, Any]:
    return _load_yaml(PACKET_PROTOCOL_PATH)


@pytest.fixture(scope="module")
def mcp_index() -> dict[str, Any]:
    return _load_yaml(MCP_INDEX_PATH)


@pytest.fixture(scope="module")
def asyncapi() -> dict[str, Any]:
    return _load_yaml(ASYNCAPI_PATH)


@pytest.fixture(scope="module")
def dependency_index() -> dict[str, Any]:
    return _load_yaml(DEPENDENCY_INDEX_PATH)


@pytest.fixture(scope="module")
def attestation_schema() -> dict[str, Any]:
    return _load_json(ATTESTATION_SCHEMA_PATH)


def test_tracked_contracts_exist(constitution: dict[str, Any]) -> None:
    missing = [
        relative_path
        for relative_path in constitution["node"]["tracked_contracts"]
        if not (ROOT / relative_path).exists()
    ]
    assert not missing, f"Missing tracked contract files: {missing}"


def test_constitution_actions_align_with_packet_protocol(
    constitution: dict[str, Any],
    packet_protocol: dict[str, Any],
) -> None:
    constitution_actions = set(constitution["actions"].keys())
    packet_actions = {item["action"] for item in packet_protocol["registered_handlers"]}
    assert constitution_actions == packet_actions


def test_constitution_tools_align_with_mcp_index(
    constitution: dict[str, Any],
    mcp_index: dict[str, Any],
) -> None:
    constitution_tools = set(constitution["tools"].keys())
    registry_tools = {item["name"] for item in mcp_index["tools"]}
    assert constitution_tools == registry_tools


def test_constitution_events_align_with_asyncapi(
    constitution: dict[str, Any],
    asyncapi: dict[str, Any],
) -> None:
    constitution_events = set(constitution["events"].keys())
    asyncapi_events = {message["name"] for message in asyncapi["components"]["messages"].values()}
    assert constitution_events == asyncapi_events


def test_constitution_dependencies_align_with_dependency_index(
    constitution: dict[str, Any],
    dependency_index: dict[str, Any],
) -> None:
    constitution_dependencies = set(constitution["dependencies"].keys())
    index_dependencies = {item["name"] for item in dependency_index["dependencies"]}
    assert constitution_dependencies.issubset(index_dependencies)


def test_writeback_action_has_external_mutation_policy(
    constitution: dict[str, Any],
) -> None:
    action = constitution["actions"]["writeback"]
    assert action["mutation_class"] == "external_mutation"
    assert action["approval_mode"] == "threshold_or_human"
    assert action["replay_safe"] is False or action["replay_safe"] == "conditional"


def test_writeback_tool_maps_to_writeback_action(
    constitution: dict[str, Any],
) -> None:
    tool = constitution["tools"]["writeback"]
    assert tool["chassis_action"] == "writeback"
    assert tool["mutation_class"] == "external_mutation"


def test_runtime_attestation_required_fields_match_schema(
    constitution: dict[str, Any],
    attestation_schema: dict[str, Any],
) -> None:
    constitution_required = set(constitution["runtime_attestation"]["required_fields"])
    schema_required = set(attestation_schema["required"])
    assert constitution_required == schema_required


def test_runtime_attestation_endpoint_declared(
    constitution: dict[str, Any],
) -> None:
    assert constitution["runtime_attestation"]["endpoint"] == "/v1/attestation"


def test_policy_mode_is_enforced(
    constitution: dict[str, Any],
) -> None:
    assert constitution["runtime_attestation"]["policy_mode"] == "enforced"


# SDK-owned operational routes that are not REST ingress: `/metrics` is the
# Prometheus scrape route constellation_node_sdk's `create_node_app` mounts.
_NON_INGRESS_PATHS = frozenset({"/metrics"})


def _under(path: str, base_path: str) -> bool:
    base = base_path.rstrip("/")
    return path == base or path.startswith(base + "/")


def _mounted_paths() -> list[str]:
    """Every documented route of the running app, `app.main:app`.

    Read from the app's own OpenAPI document, which lists each mounted route
    that is not `include_in_schema=False`; no route under `app/` sets that.
    """
    from app.main import app

    return sorted(app.openapi()["paths"])


def test_every_mounted_route_is_under_a_declared_rest_base_path(
    constitution: dict[str, Any],
) -> None:
    base_paths = constitution["ingress"]["rest"]["base_paths"]
    undeclared = [
        path
        for path in _mounted_paths()
        if path not in _NON_INGRESS_PATHS and not any(_under(path, bp) for bp in base_paths)
    ]
    assert not undeclared, (
        f"Mounted routes outside ingress.rest.base_paths {base_paths}: {undeclared}. "
        "Declare the router's prefix in docs/contracts/node.constitution.yaml."
    )


def test_every_declared_rest_base_path_is_mounted(
    constitution: dict[str, Any],
) -> None:
    mounted = _mounted_paths()
    unmounted = [
        bp
        for bp in constitution["ingress"]["rest"]["base_paths"]
        if not any(_under(path, bp) for path in mounted)
    ]
    assert not unmounted, f"Declared REST base paths with no mounted route: {unmounted}"
