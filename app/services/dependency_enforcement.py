from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.services.runtime_attestation import build_runtime_attestation

REPO_ROOT = Path(__file__).resolve().parents[2]
CONSTITUTION_PATH = REPO_ROOT / "docs/contracts/node.constitution.yaml"


class ActionDependencyError(RuntimeError):
    """Raised when an action is not executable under current dependency readiness."""


def _load_yaml(path: Path) -> dict[str, Any]:
    result = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(result) if result else {}


@lru_cache(maxsize=1)
def _constitution() -> dict[str, Any]:
    return _load_yaml(CONSTITUTION_PATH)


def evaluate_action_dependencies(
    action_name: str,
    *,
    attestation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    constitution = _constitution()
    if action_name not in constitution["actions"]:
        raise ActionDependencyError(f"unknown action: {action_name}")

    attestation = attestation if attestation is not None else build_runtime_attestation()

    action_policy = constitution["actions"][action_name]
    readiness = attestation["dependency_readiness"]

    required_dependencies = list(action_policy.get("required_dependencies", []))
    optional_dependencies = list(action_policy.get("optional_dependencies", []))

    missing_required = [
        dependency_name
        for dependency_name in required_dependencies
        if dependency_name not in readiness or not readiness[dependency_name]["ready"]
    ]

    missing_optional = [
        dependency_name
        for dependency_name in optional_dependencies
        if dependency_name not in readiness or not readiness[dependency_name]["ready"]
    ]

    allowed = len(missing_required) == 0
    block_reason: str | None = None

    if not allowed:
        block_reason = "required_dependency_unavailable"

    if action_policy["mutation_class"] == "external_mutation":
        ready_optional_targets = [
            dependency_name
            for dependency_name in optional_dependencies
            if dependency_name in readiness and readiness[dependency_name]["ready"]
        ]
        if not ready_optional_targets:
            allowed = False
            block_reason = "no_target_integration_ready"

    degraded_modes = sorted(
        {
            mode
            for dependency_name in missing_optional + missing_required
            for mode in constitution["dependencies"]
            .get(dependency_name, {})
            .get("degraded_modes", [])
        }
    )

    return {
        "action": action_name,
        "allowed": allowed,
        "block_reason": block_reason,
        "required_dependencies": required_dependencies,
        "optional_dependencies": optional_dependencies,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "degraded_modes": degraded_modes,
    }


def assert_action_dependencies(
    action_name: str,
    *,
    attestation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evaluation = evaluate_action_dependencies(action_name, attestation=attestation)
    if not evaluation["allowed"]:
        raise ActionDependencyError(
            f"action '{action_name}' blocked: {evaluation['block_reason']} "
            f"(missing_required={evaluation['missing_required']}, "
            f"missing_optional={evaluation['missing_optional']})"
        )
    return evaluation
