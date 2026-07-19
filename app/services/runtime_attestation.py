from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONSTITUTION_PATH = REPO_ROOT / "docs/contracts/node.constitution.yaml"
DEPENDENCY_INDEX_PATH = REPO_ROOT / "docs/contracts/dependencies/_index.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def _load_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _load_document(path: Path) -> dict[str, Any]:
    if path.suffix == ".json":
        return _load_json(path)
    return _load_yaml(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _constitution() -> dict[str, Any]:
    return _load_yaml(CONSTITUTION_PATH)


def _tracked_contract_hashes(constitution: dict[str, Any]) -> dict[str, str]:
    tracked: dict[str, str] = {}
    for relative_path in constitution["node"]["tracked_contracts"]:
        file_path = REPO_ROOT / relative_path
        tracked[relative_path] = _sha256_file(file_path)
    return tracked


def _contract_digest(tracked_hashes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for relative_path in sorted(tracked_hashes):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b":")
        digest.update(tracked_hashes[relative_path].encode("utf-8"))
        digest.update(b";")
    return digest.hexdigest()


def _collect_dependency_env_vars(contract: dict[str, Any]) -> list[str]:
    keys: list[str] = []

    for field_name in (
        "connection_env",
        "base_url_env",
        "user_env",
        "password_env",
        "database_env",
    ):
        value = contract.get(field_name)
        if isinstance(value, str) and value:
            keys.append(value)

    auth_env_vars = contract.get("auth_env_vars")
    if isinstance(auth_env_vars, list):
        for value in auth_env_vars:
            if isinstance(value, str) and value:
                keys.append(value)

    return sorted(set(keys))


def _dependency_readiness(constitution: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    dependency_index = _load_yaml(DEPENDENCY_INDEX_PATH)
    dependency_map = {item["name"]: item for item in dependency_index["dependencies"]}

    readiness: dict[str, Any] = {}
    degraded_modes: set[str] = set()

    for dependency_name, metadata in constitution["dependencies"].items():
        index_item = dependency_map.get(dependency_name)
        env_vars: list[str] = []
        if index_item is not None:
            contract_path = REPO_ROOT / "docs/contracts/dependencies" / index_item["file"]
            contract_doc = _load_document(contract_path)
            env_vars = _collect_dependency_env_vars(contract_doc)

        missing_env = [env_name for env_name in env_vars if not os.getenv(env_name)]
        ready = len(missing_env) == 0 or len(env_vars) == 0

        readiness[dependency_name] = {
            "required": bool(metadata["required"]),
            "ready": ready,
            "env_vars": env_vars,
            "missing_env": missing_env,
        }

        if not ready:
            for degraded_mode in metadata.get("degraded_modes", []):
                degraded_modes.add(str(degraded_mode))

    return readiness, sorted(degraded_modes)


def build_runtime_attestation() -> dict[str, Any]:
    constitution = _constitution()
    tracked_hashes = _tracked_contract_hashes(constitution)
    dependency_readiness, degraded_modes = _dependency_readiness(constitution)

    return {
        "node_id": constitution["node"]["id"],
        "node_version": constitution["node"]["version"],
        "contract_version": constitution["node"]["contract_version"],
        "contract_digest": _contract_digest(tracked_hashes),
        "generated_at": datetime.now(UTC).isoformat(),
        "tracked_contract_hashes": tracked_hashes,
        "action_inventory": sorted(constitution["actions"].keys()),
        "tool_inventory": sorted(constitution["tools"].keys()),
        "event_inventory": sorted(constitution["events"].keys()),
        "dependency_readiness": dependency_readiness,
        "degraded_modes": degraded_modes,
        "policy_mode": constitution["runtime_attestation"]["policy_mode"],
    }
