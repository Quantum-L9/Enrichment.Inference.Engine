"""Automated enforcement of live SDK transport/runtime architecture rules."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
APP_DIR = REPO_ROOT / "app"

ACTIVE_TRANSPORT_BUNDLE = [
    REPO_ROOT / "app" / "main.py",
    REPO_ROOT / "app" / "api" / "v1" / "chassis_endpoint.py",
    REPO_ROOT / "app" / "services" / "chassis_handlers.py",
    REPO_ROOT / "app" / "engines" / "orchestration_layer.py",
    REPO_ROOT / "app" / "engines" / "handlers.py",
    REPO_ROOT / "app" / "engines" / "graph_sync_client.py",
]

DEPRECATED_COMPAT_ARTIFACTS = [
    "chassis/envelope.py",
    "chassis/router.py",
    "chassis/registry.py",
]


def test_required_directories_exist() -> None:
    required = [
        "app",
        "app/api",
        "app/engines",
        "app/models",
        "app/services",
        "app/score",
        "app/health",
        "tests",
        "tests/ci",
        "tests/compliance",
        "kb",
        "config",
        "tools",
    ]
    missing = [path for path in required if not (REPO_ROOT / path).exists()]
    assert not missing, f"Missing directories: {missing}"


def test_required_init_files() -> None:
    required = [
        "app/__init__.py",
        "app/api/__init__.py",
        "app/engines/__init__.py",
        "app/models/__init__.py",
        "app/services/__init__.py",
        "app/score/__init__.py",
        "app/health/__init__.py",
    ]
    missing = [path for path in required if not (REPO_ROOT / path).exists()]
    assert not missing, f"Missing __init__.py files: {missing}"


def test_kb_yaml_files_valid() -> None:
    kb_dir = REPO_ROOT / "kb"
    if not kb_dir.exists():
        pytest.skip("No kb/ directory present")

    import yaml

    invalid: list[str] = []
    for yaml_file in kb_dir.rglob("*.yaml"):
        try:
            data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if data is None:
                invalid.append(f"{yaml_file.relative_to(REPO_ROOT)}: empty file")
        except yaml.YAMLError as exc:
            invalid.append(f"{yaml_file.relative_to(REPO_ROOT)}: {exc}")

    assert not invalid, "Invalid YAML files:\n" + "\n".join(invalid)


def test_active_transport_bundle_files_exist() -> None:
    missing = [
        str(path.relative_to(REPO_ROOT)) for path in ACTIVE_TRANSPORT_BUNDLE if not path.exists()
    ]
    assert not missing, f"Missing active transport/runtime bundle files: {missing}"


def test_sdk_runtime_owns_production_transport_ingress() -> None:
    main_module = REPO_ROOT / "app" / "main.py"
    content = main_module.read_text(encoding="utf-8")

    assert "create_node_app" in content, "app/main.py must create the SDK node runtime"
    assert "NodeRuntimeConfig" in content, "app/main.py must define SDK runtime configuration"
    assert "allowed_actions" in content, "app/main.py must declare allowed runtime actions"


def test_supplemental_transport_route_does_not_own_execute() -> None:
    endpoint_module = REPO_ROOT / "app" / "api" / "v1" / "chassis_endpoint.py"
    content = endpoint_module.read_text(encoding="utf-8")

    assert '"/v1/execute"' not in content, "chassis_endpoint.py must not define /v1/execute"
    assert '"/v1/outcomes"' in content, (
        "supplemental transport-adjacent routes must remain explicit"
    )


def test_active_runtime_bundle_does_not_import_deprecated_router_or_registry() -> None:
    violations: list[str] = []

    for path in ACTIVE_TRANSPORT_BUNDLE:
        content = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT)
        if "chassis.router" in content:
            violations.append(f"{rel}: imports deprecated chassis.router")
        if "chassis.registry" in content:
            violations.append(f"{rel}: imports deprecated chassis.registry")

    assert not violations, "Deprecated dispatch imports found:\n" + "\n".join(violations)


def test_deprecated_compatibility_artifacts_are_not_treated_as_active_runtime_requirements() -> (
    None
):
    # REPO_MAP.md and ARCHITECTURE.md live under docs/; AGENTS.md at repo root.
    repo_map = (REPO_ROOT / "docs" / "REPO_MAP.md").read_text(encoding="utf-8")
    architecture = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")

    for rel_path in DEPRECATED_COMPAT_ARTIFACTS:
        assert rel_path in repo_map, f"{rel_path} must be listed in REPO_MAP.md as deprecated"
        assert rel_path in architecture, (
            f"{rel_path} must be listed in ARCHITECTURE.md as deprecated"
        )
        assert rel_path in agents, f"{rel_path} must be listed in AGENTS.md as deprecated"
