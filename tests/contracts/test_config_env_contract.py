"""
Config / Env Contract Tests
Source: app/core/config.py Settings, docs/contracts/config/env-contract.yaml, .env.example
Markers: unit
"""

from __future__ import annotations

import pytest

from tests.contracts.conftest_contracts import CONFIG_DIR, REPO_ROOT, load_yaml

# Every name must appear in env-contract.yaml `variables` (1:1 with Settings).
REQUIRED_ENV_VARS = {
    "PERPLEXITY_API_KEY": {"type": "secret", "required": False, "sensitive": True},
    "PERPLEXITY_MODEL": {"type": "string", "required": False, "sensitive": False},
    "API_SECRET_KEY": {"type": "secret", "required": False, "sensitive": True},
    "API_KEY_HASH": {"type": "string", "required": False, "sensitive": False},
    "KB_DIR": {"type": "string", "required": False, "sensitive": False},
    "REDIS_URL": {"type": "url", "required": False, "sensitive": False},
    "DEFAULT_CONSENSUS_THRESHOLD": {"type": "float", "required": False, "sensitive": False},
    "DEFAULT_MAX_VARIATIONS": {"type": "integer", "required": False, "sensitive": False},
    "DEFAULT_TIMEOUT_SECONDS": {"type": "integer", "required": False, "sensitive": False},
    "MAX_CONCURRENT_VARIATIONS": {"type": "integer", "required": False, "sensitive": False},
    "MAX_ENTITIES_PER_BATCH": {"type": "integer", "required": False, "sensitive": False},
    "CB_FAILURE_THRESHOLD": {"type": "integer", "required": False, "sensitive": False},
    "CB_COOLDOWN_SECONDS": {"type": "integer", "required": False, "sensitive": False},
    "ODOO_URL": {"type": "url", "required": False, "sensitive": False},
    "ODOO_DB": {"type": "string", "required": False, "sensitive": False},
    "ODOO_USERNAME": {"type": "string", "required": False, "sensitive": False},
    "ODOO_PASSWORD": {"type": "secret", "required": False, "sensitive": True},
    "CRM_MAPPING_PATH": {"type": "string", "required": False, "sensitive": False},
    "SALESFORCE_CLIENT_ID": {"type": "secret", "required": False, "sensitive": True},
    "SALESFORCE_CLIENT_SECRET": {"type": "secret", "required": False, "sensitive": True},
    "SALESFORCE_USERNAME": {"type": "string", "required": False, "sensitive": False},
    "SALESFORCE_PASSWORD": {"type": "secret", "required": False, "sensitive": True},
    "SALESFORCE_SECURITY_TOKEN": {"type": "secret", "required": False, "sensitive": True},
    "HUBSPOT_ACCESS_TOKEN": {"type": "secret", "required": False, "sensitive": True},
    "CLEARBIT_API_KEY": {"type": "secret", "required": False, "sensitive": True},
    "ZOOMINFO_API_KEY": {"type": "secret", "required": False, "sensitive": True},
    "APOLLO_API_KEY": {"type": "secret", "required": False, "sensitive": True},
    "HUNTER_API_KEY": {"type": "secret", "required": False, "sensitive": True},
    "OPENAI_API_KEY": {"type": "secret", "required": False, "sensitive": True},
    "ANTHROPIC_API_KEY": {"type": "secret", "required": False, "sensitive": True},
    "GATE_URL": {"type": "url", "required": False, "sensitive": False},
    "GATE_REGISTRATION_ENABLED": {"type": "boolean", "required": False, "sensitive": False},
    "GATE_INTERNAL_URL": {"type": "url", "required": False, "sensitive": False},
    "GATE_ADMIN_TOKEN": {"type": "secret", "required": False, "sensitive": True},
    # C-09 names for the controls PR #212 introduced (EIE-212-F004).
    "L9_ENRICHMENT_PROVIDER": {"type": "string", "required": False, "sensitive": False},
    "L9_GATE_REREGISTRATION_INTERVAL_SECONDS": {
        "type": "number",
        "required": False,
        "sensitive": False,
    },
    "L9_ENVIRONMENT": {"type": "string", "required": False, "sensitive": False},
    "GRAPH_SYNC_ENTITY_TYPE": {"type": "string", "required": False, "sensitive": False},
    "GRAPH_SYNC_ID_PROPERTY": {"type": "string", "required": False, "sensitive": False},
    "DATABASE_URL": {"type": "url", "required": False, "sensitive": True},
    "DOMAINS_DIR": {"type": "string", "required": False, "sensitive": False},
    "DEFAULT_DOMAIN": {"type": "string", "required": False, "sensitive": False},
    "MAX_BUDGET_TOKENS": {"type": "integer", "required": False, "sensitive": False},
    "MAX_BUDGET_TOKENS_DEFAULT": {"type": "integer", "required": False, "sensitive": False},
    "TOKEN_RATE_USD_PER_1K": {"type": "float", "required": False, "sensitive": False},
    "LOG_LEVEL": {"type": "string", "required": False, "sensitive": False},
}


@pytest.fixture(scope="module")
def env_contract() -> dict:
    path = CONFIG_DIR / "env-contract.yaml"
    if not path.exists():
        pytest.skip("env-contract.yaml missing")
    return load_yaml(path)


@pytest.mark.unit
def test_env_contract_is_valid_yaml(env_contract: dict) -> None:
    assert isinstance(env_contract, dict)


@pytest.mark.unit
@pytest.mark.parametrize("var_name", REQUIRED_ENV_VARS.keys())
def test_env_var_documented(var_name: str, env_contract: dict) -> None:
    assert var_name in str(env_contract), (
        f"Env var '{var_name}' not documented in env-contract.yaml. "
        "Source: app/core/config.py Settings class"
    )


@pytest.mark.unit
@pytest.mark.parametrize("var_name,meta", REQUIRED_ENV_VARS.items())
def test_sensitive_vars_marked(var_name: str, meta: dict, env_contract: dict) -> None:
    if not meta.get("sensitive"):
        return
    s = str(env_contract)
    if var_name not in s:
        pytest.skip(f"{var_name} not in contract")
    assert "sensitive" in s.lower(), (
        f"env-contract.yaml must mark sensitive vars. {var_name} is sensitive — Phase 1.5"
    )


@pytest.mark.unit
def test_env_contract_has_source_annotation(env_contract: dict) -> None:
    s = str(env_contract).lower()
    assert "app/core/config.py" in s or "config.py" in s or "source" in s, (
        "env-contract.yaml must trace to app/core/config.py — Phase 3.6 Rule 1"
    )


@pytest.mark.unit
def test_env_contract_has_examples(env_contract: dict) -> None:
    s = str(env_contract)
    has_examples = "example" in s.lower()
    assert has_examples, "env-contract.yaml must include examples — Phase 3.6 Rule 3"


@pytest.mark.unit
def test_required_for_startup_lists_auth_and_enrichment(env_contract: dict) -> None:
    startup = env_contract.get("required_for_startup", [])
    assert "API_KEY_HASH" in startup
    assert "PERPLEXITY_API_KEY" in startup


# Seam audit 2026-09-02: direct peer addressing and the graph store are gone
# from EIE's configuration surface. Gate is the only peer EIE addresses.
RETIRED_SIDE_DOOR_VARS = {
    "CEG_BASE_URL",
    "GRAPH_NODE_URL",
    "SCORE_NODE_URL",
    "ROUTE_NODE_URL",
    "INTER_NODE_SECRET",
    "NEO4J_URI",
    "NEO4J_USER",
    "NEO4J_PASSWORD",
}


@pytest.mark.unit
def test_retired_peer_variables_are_gone_from_settings_and_examples() -> None:
    from app.core.config import Settings

    fields = {name.upper() for name in Settings.model_fields}
    assert not (fields & RETIRED_SIDE_DOOR_VARS), fields & RETIRED_SIDE_DOOR_VARS
    for rel in (".env.example", ".env.local.example", "docker-compose.yml"):
        text = (REPO_ROOT / rel).read_text()
        for var in RETIRED_SIDE_DOOR_VARS:
            assert f"{var}=" not in text and f"{var}:" not in text, f"{rel} still sets {var}"


# C-09 (AGENTS.md): new application env vars carry the L9_ prefix. The
# unprefixed legacy set predates the rule and is not migrated here; these are the
# controls PR #212 added, so they must comply (EIE-212-F004).
NEW_L9_CONTROLS = {
    "l9_enrichment_provider": "L9_ENRICHMENT_PROVIDER",
    "l9_gate_reregistration_interval_seconds": "L9_GATE_REREGISTRATION_INTERVAL_SECONDS",
}
RETIRED_UNPREFIXED_NAMES = ("ENRICHMENT_PROVIDER", "GATE_REREGISTRATION_INTERVAL_SECONDS")
CONFIG_SURFACES = (
    ".env.example",
    "docs/contracts/config/env-contract.yaml",
    "infra/k8s/kustomize/base/kustomization.yaml",
)


@pytest.mark.unit
def test_new_controls_are_c09_compliant_settings_fields() -> None:
    from app.core.config import Settings

    for field, env_name in NEW_L9_CONTROLS.items():
        assert field in Settings.model_fields, f"Settings.{field} missing"
        assert field.upper() == env_name
        assert env_name.startswith("L9_"), f"{env_name} violates C-09"
    for field in ("enrichment_provider", "gate_reregistration_interval_seconds"):
        assert field not in Settings.model_fields, f"unprefixed Settings.{field} still exists"


@pytest.mark.unit
def test_new_controls_are_read_from_their_l9_env_names(monkeypatch) -> None:
    """pydantic-settings maps the field name to the env var; prove the external name works."""
    from app.core.config import Settings

    monkeypatch.setenv("L9_ENRICHMENT_PROVIDER", "deterministic")
    monkeypatch.setenv("L9_ENVIRONMENT", "test")
    monkeypatch.setenv("L9_GATE_REREGISTRATION_INTERVAL_SECONDS", "7.5")
    settings = Settings(_env_file=None)
    assert settings.l9_enrichment_provider == "deterministic"
    assert settings.l9_gate_reregistration_interval_seconds == 7.5


@pytest.mark.unit
def test_retired_unprefixed_names_are_gone_from_config_surfaces() -> None:
    for rel in CONFIG_SURFACES:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for name in RETIRED_UNPREFIXED_NAMES:
            # Match the bare name as a variable, not as the suffix of its L9_ form:
            # a dotenv line, a kustomize literal, and an env-contract entry.
            assert f"\n{name}=" not in text, f"{rel} still carries dotenv {name}="
            assert f"- {name}=" not in text, f"{rel} still carries literal {name}="
            assert f"name: {name}\n" not in text, f"{rel} still documents {name}"


@pytest.mark.unit
def test_dotenv_example_aligns_with_contract() -> None:
    dotenv_path = REPO_ROOT / ".env.example"
    if not dotenv_path.exists():
        pytest.skip(".env.example not found at repo root")
    dotenv_vars = set()
    for line in dotenv_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            dotenv_vars.add(line.split("=")[0].strip())
    env_path = CONFIG_DIR / "env-contract.yaml"
    if not env_path.exists():
        pytest.skip("env-contract.yaml missing")
    contract_str = env_path.read_text()
    undocumented = [v for v in dotenv_vars if v not in contract_str and v.strip()]
    assert not undocumented, (
        f".env.example vars not in env-contract.yaml: {undocumented}. "
        "Every env var in .env.example must be documented."
    )
