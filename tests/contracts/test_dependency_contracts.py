"""
Dependency Contract Tests
Source: .env.example, docker-compose.yml, app/services/
Markers: unit
"""

from __future__ import annotations

import pytest

from tests.contracts.conftest_contracts import DEPS_DIR, load_yaml

EXPECTED_DEPS = {
    "perplexity-sonar": {"env_key": "PERPLEXITY_API_KEY", "type": "external"},
    "openai": {"env_key": None, "type": "external"},
    "anthropic": {"env_key": None, "type": "external"},
    "clearbit": {"env_key": "CLEARBIT_API_KEY", "type": "external"},
    "zoominfo": {"env_key": "ZOOMINFO_API_KEY", "type": "external"},
    "apollo": {"env_key": "APOLLO_API_KEY", "type": "external"},
    "hunter": {"env_key": "HUNTER_API_KEY", "type": "external"},
    "odoo-crm": {"env_key": "ODOO_URL", "type": "external"},
    "salesforce-crm": {"env_key": "SALESFORCE_CLIENT_ID", "type": "external"},
    "hubspot-crm": {"env_key": "HUBSPOT_ACCESS_TOKEN", "type": "external"},
    "redis": {"env_key": "REDIS_URL", "type": "internal"},
    "postgresql": {"env_key": None, "type": "internal"},
    # Seam audit 2026-09-02: EIE has no graph-store client. Its graph dependency
    # is Constellation.Gate (Gate-routed `sync` / `match` / `outcomes` to CEG).
    "constellation-gate": {"env_key": "GATE_URL", "type": "internal"},
}

REQUIRED_FIELDS = ["service_name", "type", "protocol", "auth_method"]


@pytest.mark.unit
def test_direct_graph_store_dependency_is_retired() -> None:
    assert not (DEPS_DIR / "neo4j.yaml").exists(), (
        "EIE reaches the graph only through Gate; a Neo4j dependency contract is a side door"
    )


@pytest.mark.unit
@pytest.mark.parametrize("dep_name", EXPECTED_DEPS.keys())
def test_dependency_file_exists(dep_name: str) -> None:
    assert (DEPS_DIR / f"{dep_name}.yaml").exists(), f"Missing: dependencies/{dep_name}.yaml"


@pytest.fixture(scope="module")
def all_dep_contracts() -> dict:
    result = {}
    for dep_name in EXPECTED_DEPS:
        path = DEPS_DIR / f"{dep_name}.yaml"
        if path.exists():
            result[dep_name] = load_yaml(path)
    return result


@pytest.mark.unit
@pytest.mark.parametrize("field", REQUIRED_FIELDS)
@pytest.mark.parametrize("dep_name", EXPECTED_DEPS.keys())
def test_dependency_has_required_field(dep_name: str, field: str, all_dep_contracts: dict) -> None:
    if dep_name not in all_dep_contracts:
        pytest.skip(f"{dep_name}.yaml missing")
    assert field in str(all_dep_contracts[dep_name]), f"{dep_name}.yaml: missing field '{field}'"


@pytest.mark.unit
@pytest.mark.parametrize("dep_name", EXPECTED_DEPS.keys())
def test_dependency_type_is_valid(dep_name: str, all_dep_contracts: dict) -> None:
    if dep_name not in all_dep_contracts:
        pytest.skip(f"{dep_name}.yaml missing")
    dep_type = all_dep_contracts[dep_name].get("type")
    if dep_type:
        assert dep_type in ("internal", "external")


@pytest.mark.unit
@pytest.mark.parametrize("dep_name,meta", EXPECTED_DEPS.items())
def test_dependency_type_matches_expectation(
    dep_name: str, meta: dict, all_dep_contracts: dict
) -> None:
    if dep_name not in all_dep_contracts:
        pytest.skip(f"{dep_name}.yaml missing")
    declared = all_dep_contracts[dep_name].get("type")
    if declared:
        assert declared == meta["type"], (
            f"{dep_name}.yaml: type={declared!r}, expected {meta['type']!r}"
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "dep_name,meta", {k: v for k, v in EXPECTED_DEPS.items() if v["env_key"]}.items()
)
def test_dependency_env_key_referenced(dep_name: str, meta: dict, all_dep_contracts: dict) -> None:
    if dep_name not in all_dep_contracts:
        pytest.skip(f"{dep_name}.yaml missing")
    assert meta["env_key"] in str(all_dep_contracts[dep_name]), (
        f"{dep_name}.yaml: missing reference to env var '{meta['env_key']}'"
    )


@pytest.mark.unit
def test_deps_index_covers_all_dependencies() -> None:
    path = DEPS_DIR / "_index.yaml"
    if not path.exists():
        pytest.skip("_index.yaml missing")
    index_str = str(load_yaml(path)).lower()
    for dep_name in EXPECTED_DEPS:
        stem = dep_name.split("-")[0]
        assert stem in index_str, f"Dependency '{dep_name}' not in dependencies/_index.yaml"
