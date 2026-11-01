from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.api.v1.attestation import get_runtime_attestation
from app.services.runtime_attestation import build_runtime_attestation

pytestmark = [pytest.mark.unit, pytest.mark.enforcement, pytest.mark.provenance]

ROOT = Path(".")
CONSTITUTION_PATH = ROOT / "docs/contracts/node.constitution.yaml"
ATTESTATION_SCHEMA_PATH = ROOT / "docs/contracts/runtime-attestation.schema.json"


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def constitution() -> dict[str, Any]:
    return _load_yaml(CONSTITUTION_PATH)


@pytest.fixture(scope="module")
def attestation_schema() -> dict[str, Any]:
    return _load_json(ATTESTATION_SCHEMA_PATH)


def test_runtime_attestation_contains_required_fields(
    constitution: dict[str, Any],
) -> None:
    attestation = build_runtime_attestation()
    missing = [
        field
        for field in constitution["runtime_attestation"]["required_fields"]
        if field not in attestation
    ]
    assert not missing, f"Runtime attestation missing required fields: {missing}"


def test_runtime_attestation_matches_schema_required_fields(
    attestation_schema: dict[str, Any],
) -> None:
    attestation = build_runtime_attestation()
    missing = [field for field in attestation_schema["required"] if field not in attestation]
    assert not missing, f"Runtime attestation missing schema fields: {missing}"


def test_contract_digest_is_stable_with_same_filesystem_state() -> None:
    first = build_runtime_attestation()
    second = build_runtime_attestation()
    assert first["contract_digest"] == second["contract_digest"]


def test_action_inventory_matches_constitution(
    constitution: dict[str, Any],
) -> None:
    attestation = build_runtime_attestation()
    assert attestation["action_inventory"] == sorted(constitution["actions"].keys())


def test_tool_inventory_matches_constitution(
    constitution: dict[str, Any],
) -> None:
    attestation = build_runtime_attestation()
    assert attestation["tool_inventory"] == sorted(constitution["tools"].keys())


def test_event_inventory_matches_constitution(
    constitution: dict[str, Any],
) -> None:
    attestation = build_runtime_attestation()
    assert attestation["event_inventory"] == sorted(constitution["events"].keys())


def test_dependency_readiness_covers_constitution_dependencies(
    constitution: dict[str, Any],
) -> None:
    attestation = build_runtime_attestation()
    assert set(attestation["dependency_readiness"].keys()) == set(
        constitution["dependencies"].keys()
    )


def test_tracked_contract_hashes_cover_constitution_tracked_files(
    constitution: dict[str, Any],
) -> None:
    attestation = build_runtime_attestation()
    assert set(attestation["tracked_contract_hashes"].keys()) == set(
        constitution["node"]["tracked_contracts"]
    )


def test_route_returns_runtime_attestation_payload(
    constitution: dict[str, Any],
) -> None:
    payload = get_runtime_attestation()
    for field in constitution["runtime_attestation"]["required_fields"]:
        assert field in payload


def test_hubspot_readiness_requires_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # HubSpotClient authenticates with Settings.hubspot_access_token
    # (HUBSPOT_ACCESS_TOKEN). A contract that declares no env var is counted ready
    # unconditionally, so HubSpot must declare its token to be attested honestly.
    monkeypatch.delenv("HUBSPOT_ACCESS_TOKEN", raising=False)
    unset = build_runtime_attestation()["dependency_readiness"]["HubSpotCRM"]
    assert unset["env_vars"] == ["HUBSPOT_ACCESS_TOKEN"]
    assert unset["missing_env"] == ["HUBSPOT_ACCESS_TOKEN"]
    assert unset["ready"] is False

    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "attestation-fixture")
    set_ = build_runtime_attestation()["dependency_readiness"]["HubSpotCRM"]
    assert set_["missing_env"] == []
    assert set_["ready"] is True
