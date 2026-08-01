from __future__ import annotations

from app.utils.safe_convert import safe_float
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.services.dependency_enforcement import assert_action_dependencies

REPO_ROOT = Path(__file__).resolve().parents[2]
CONSTITUTION_PATH = REPO_ROOT / "docs/contracts/node.constitution.yaml"
WRITEBACK_SCHEMA_PATH = REPO_ROOT / "docs/contracts/agents/tool-schemas/writeback.schema.json"


class ActionAuthorizationError(PermissionError):
    """Raised when an action or tool violates constitution-defined authority."""


def _load_yaml(path: Path) -> dict[str, Any]:
    result = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(result) if result else {}


def _load_json(path: Path) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8"))
    return dict(result) if result else {}


@lru_cache(maxsize=1)
def _constitution() -> dict[str, Any]:
    return _load_yaml(CONSTITUTION_PATH)


@lru_cache(maxsize=1)
def _writeback_schema() -> dict[str, Any]:
    return _load_json(WRITEBACK_SCHEMA_PATH)


def get_action_policy(action_name: str) -> dict[str, Any]:
    constitution = _constitution()
    try:
        return dict(constitution["actions"][action_name])
    except KeyError as exc:
        raise ActionAuthorizationError(f"unknown action: {action_name}") from exc


def get_tool_policy(tool_name: str) -> dict[str, Any]:
    constitution = _constitution()
    try:
        return dict(constitution["tools"][tool_name])
    except KeyError as exc:
        raise ActionAuthorizationError(f"unknown tool: {tool_name}") from exc


def authorize_tool(tool_name: str) -> dict[str, Any]:
    return get_tool_policy(tool_name)


def evaluate_writeback_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ActionAuthorizationError("writeback payload must be an object")

    schema = _writeback_schema()
    missing_required = [field for field in schema["required"] if field not in payload]
    if missing_required:
        raise ActionAuthorizationError(
            f"writeback payload missing required fields: {sorted(missing_required)}"
        )

    enriched_data = payload.get("enriched_data")
    if not isinstance(enriched_data, dict):
        raise ActionAuthorizationError("writeback enriched_data must be an object")

    threshold = safe_float(payload.get("confidence_threshold", 0.70))
    if threshold < 0.0 or threshold > 1.0:
        raise ActionAuthorizationError("writeback confidence_threshold must be within [0, 1]")

    field_confidences = payload.get("_field_confidences", {})
    if field_confidences is None:
        field_confidences = {}
    if not isinstance(field_confidences, dict):
        raise ActionAuthorizationError(
            "writeback _field_confidences must be an object when present"
        )

    written_fields: list[str] = []
    skipped_fields: list[str] = []
    skip_reasons: dict[str, str] = {}

    for field_name in enriched_data:
        confidence = safe_float(field_confidences.get(field_name, 0.0))
        if confidence >= threshold:
            written_fields.append(field_name)
        else:
            skipped_fields.append(field_name)
            skip_reasons[field_name] = (
                f"confidence {confidence:.4f} below threshold {threshold:.4f}"
            )

    if written_fields and skipped_fields:
        status = "partial"
    elif written_fields:
        status = "completed"
    else:
        status = "rejected"

    return {
        "status": status,
        "attempted_fields": list(enriched_data.keys()),
        "written_fields": written_fields,
        "skipped_fields": skipped_fields,
        "skip_reasons": skip_reasons,
    }


def authorize_action(
    action_name: str,
    *,
    payload: dict[str, Any] | None = None,
    policy_cleared: bool = False,
    attestation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action_policy = get_action_policy(action_name)
    dependency_state = assert_action_dependencies(action_name, attestation=attestation)

    if action_policy["mutation_class"] == "external_mutation" and not policy_cleared:
        raise ActionAuthorizationError(f"action '{action_name}' requires explicit policy clearance")

    writeback_evaluation: dict[str, Any] | None = None
    if action_name == "writeback":
        if payload is None:
            raise ActionAuthorizationError("writeback action requires payload")
        writeback_evaluation = evaluate_writeback_payload(payload)

    return {
        "action": action_name,
        "policy": action_policy,
        "dependency_state": dependency_state,
        "writeback_evaluation": writeback_evaluation,
    }
