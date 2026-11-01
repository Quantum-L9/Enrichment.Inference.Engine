#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSTITUTION_PATH = REPO_ROOT / "docs/contracts/node.constitution.yaml"
PACKET_PROTOCOL_PATH = REPO_ROOT / "docs/contracts/agents/protocols/packet-envelope.yaml"
MCP_INDEX_PATH = REPO_ROOT / "docs/contracts/agents/tool-schemas/_index.yaml"
ASYNCAPI_PATH = REPO_ROOT / "docs/contracts/events/asyncapi.yaml"
DEPENDENCY_INDEX_PATH = REPO_ROOT / "docs/contracts/dependencies/_index.yaml"
ATTESTATION_SCHEMA_PATH = REPO_ROOT / "docs/contracts/runtime-attestation.schema.json"
CI_GATE_PATH = REPO_ROOT / "docs/contracts/enforcement/ci-gate.yaml"
REVIEW_POLICY_PATH = REPO_ROOT / "docs/contracts/enforcement/review-policy.yaml"
AGENT_POLICY_PATH = REPO_ROOT / "docs/contracts/enforcement/agent-policy.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return result


def _load_json(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return result


def _read_constitution() -> dict[str, Any]:
    return _load_yaml(CONSTITUTION_PATH)


def _git_changed_files(base: str, head: str) -> list[str]:
    output = subprocess.check_output(
        ["git", "diff", "--name-only", base, head],
        cwd=REPO_ROOT,
        text=True,
    )
    return [line.strip() for line in output.splitlines() if line.strip()]


def _matches_prefix(path: str, prefixes: list[str]) -> bool:
    return any(path.startswith(prefix) for prefix in prefixes)


def _relative_exists(relative_path: str) -> bool:
    return (REPO_ROOT / relative_path).exists()


@dataclass(frozen=True)
class GateSelection:
    ids: list[str]
    commands: list[str]


def verify_constitution() -> tuple[bool, list[str]]:
    errors: list[str] = []

    constitution = _read_constitution()
    packet_protocol = _load_yaml(PACKET_PROTOCOL_PATH)
    mcp_index = _load_yaml(MCP_INDEX_PATH)
    asyncapi = _load_yaml(ASYNCAPI_PATH)
    dependency_index = _load_yaml(DEPENDENCY_INDEX_PATH)
    attestation_schema = _load_json(ATTESTATION_SCHEMA_PATH)

    tracked_files = constitution["node"]["tracked_contracts"]
    for relative_path in tracked_files:
        if not _relative_exists(relative_path):
            errors.append(f"tracked contract missing: {relative_path}")

    constitution_actions = set(constitution["actions"].keys())
    packet_actions = {item["action"] for item in packet_protocol["registered_handlers"]}
    if constitution_actions != packet_actions:
        errors.append(
            f"action inventory mismatch: constitution={sorted(constitution_actions)} "
            f"packet={sorted(packet_actions)}"
        )

    constitution_tools = set(constitution["tools"].keys())
    mcp_tools = {item["name"] for item in mcp_index["tools"]}
    if constitution_tools != mcp_tools:
        errors.append(
            f"tool inventory mismatch: constitution={sorted(constitution_tools)} "
            f"mcp={sorted(mcp_tools)}"
        )

    constitution_events = set(constitution["events"].keys())
    asyncapi_events = {item["name"] for item in asyncapi["components"]["messages"].values()}
    if constitution_events != asyncapi_events:
        errors.append(
            f"event inventory mismatch: constitution={sorted(constitution_events)} "
            f"asyncapi={sorted(asyncapi_events)}"
        )

    constitution_dependencies = set(constitution["dependencies"].keys())
    dependency_names = {item["name"] for item in dependency_index["dependencies"]}
    if not constitution_dependencies.issubset(dependency_names):
        errors.append(
            "constitution dependencies missing from dependency index: "
            f"{sorted(constitution_dependencies - dependency_names)}"
        )

    writeback_action = constitution["actions"].get("writeback", {})
    if writeback_action.get("mutation_class") != "external_mutation":
        errors.append("writeback action must be external_mutation")
    if writeback_action.get("approval_mode") != "threshold_or_human":
        errors.append("writeback action must require threshold_or_human approval")

    required_fields = set(constitution["runtime_attestation"]["required_fields"])
    schema_required = set(attestation_schema["required"])
    if required_fields != schema_required:
        errors.append(
            f"runtime attestation required fields mismatch: constitution={sorted(required_fields)} "
            f"schema={sorted(schema_required)}"
        )

    return len(errors) == 0, errors


def verify_attestation() -> tuple[bool, list[str]]:
    errors: list[str] = []
    # Run as `python scripts/l9_contract_control.py`, sys.path[0] is scripts/,
    # not the repository root, so `app` was unimportable in any job that does
    # not pip-install the package — and that failure used to read as a SKIP.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        from app.services.runtime_attestation import build_runtime_attestation
    except Exception as exc:
        # F-029: an attestation that could not be built was not verified. A
        # missing app module (or any transitive dependency) is a FAIL, never a
        # silent PASS; install the app before running this gate.
        return False, [f"unable to import runtime attestation builder: {exc!r}"]

    constitution = _read_constitution()
    schema = _load_json(ATTESTATION_SCHEMA_PATH)
    payload = build_runtime_attestation()

    missing_constitution = [
        field
        for field in constitution["runtime_attestation"]["required_fields"]
        if field not in payload
    ]
    if missing_constitution:
        errors.append(f"attestation missing constitution fields: {missing_constitution}")

    missing_schema = [field for field in schema["required"] if field not in payload]
    if missing_schema:
        errors.append(f"attestation missing schema fields: {missing_schema}")

    if payload.get("action_inventory") != sorted(constitution["actions"].keys()):
        errors.append("attestation action_inventory mismatch")

    if payload.get("tool_inventory") != sorted(constitution["tools"].keys()):
        errors.append("attestation tool_inventory mismatch")

    if payload.get("event_inventory") != sorted(constitution["events"].keys()):
        errors.append("attestation event_inventory mismatch")

    return len(errors) == 0, errors


def select_gates(changed_files: list[str]) -> GateSelection:
    spec = _load_yaml(CI_GATE_PATH)
    selected_ids: list[str] = []
    commands: list[str] = []

    for gate_id in spec["gate_order"]:
        gate = spec["gates"][gate_id]
        if any(_matches_prefix(path, gate["match_any_prefix"]) for path in changed_files):
            selected_ids.append(gate_id)
            commands.extend(gate["commands"])

    return GateSelection(ids=selected_ids, commands=commands)


def build_review_signal(changed_files: list[str]) -> tuple[bool, str]:
    policy = _load_yaml(REVIEW_POLICY_PATH)
    fail = False
    lines: list[str] = []
    lines.append("## L9 Contract Control Review Signal")
    lines.append("")
    lines.append("### Changed files")
    for path in changed_files:
        lines.append(f"- `{path}`")

    contract_bound = [
        path for path in changed_files if _matches_prefix(path, policy["contract_bound_prefixes"])
    ]
    companion_changed = [
        path for path in changed_files if _matches_prefix(path, policy["companion_prefixes"])
    ]

    lines.append("")
    lines.append("### Contract-bound changes")
    if contract_bound:
        for path in contract_bound:
            lines.append(f"- `{path}`")
    else:
        lines.append("- none")

    lines.append("")
    lines.append("### Companion changes")
    if companion_changed:
        for path in companion_changed:
            lines.append(f"- `{path}`")
    else:
        lines.append("- none")

    if (
        policy["review_signal"]["fail_if_contract_bound_without_companion"]
        and contract_bound
        and not companion_changed
    ):
        fail = True
        lines.append("")
        lines.append("### Blocking finding")
        lines.append(
            "- contract-bound files changed without companion contract/test/control updates"
        )

    if policy["review_signal"]["fail_if_required_companion_missing"] and contract_bound:
        missing_required = [
            path
            for path in policy["required_companion_files"]
            if path not in changed_files and not _relative_exists(path)
        ]
        if missing_required:
            fail = True
            lines.append("")
            lines.append("### Blocking finding")
            for path in missing_required:
                lines.append(f"- required companion file missing from repository: `{path}`")

    lines.append("")
    lines.append("### Surface checks")
    for surface_name, surface_rule in policy["surface_rules"].items():
        surface_hits = [
            path
            for path in changed_files
            if _matches_prefix(path, surface_rule["match_any_prefix"])
        ]
        if not surface_hits:
            continue
        lines.append(f"- `{surface_name}` impacted")
        companion_hits = [
            path
            for path in changed_files
            if _matches_prefix(path, surface_rule["must_update_any_prefix"])
        ]
        if not companion_hits:
            fail = True
            lines.append(
                f"  - missing required co-change under: {surface_rule['must_update_any_prefix']}"
            )
        for check in surface_rule["review_checks"]:
            lines.append(f"  - review check: `{check}`")

    return (not fail), "\n".join(lines)


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="L9 contract control CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("verify-constitution")
    subparsers.add_parser("verify-attestation")

    select_parser = subparsers.add_parser("select-gates")
    select_parser.add_argument("--base", type=str)
    select_parser.add_argument("--head", type=str)
    select_parser.add_argument("--files", nargs="*", default=[])

    review_parser = subparsers.add_parser("review-signal")
    review_parser.add_argument("--base", type=str)
    review_parser.add_argument("--head", type=str)
    review_parser.add_argument("--files", nargs="*", default=[])

    return parser.parse_args()


def _resolve_changed_files(args: argparse.Namespace) -> list[str]:
    if getattr(args, "files", None):
        return [path for path in args.files if path]
    if getattr(args, "base", None) and getattr(args, "head", None):
        return _git_changed_files(args.base, args.head)
    return []


def main() -> int:
    args = _parse_args()

    if args.command == "verify-constitution":
        ok, errors = verify_constitution()
        if ok:
            print("[l9-control] constitution verification passed")
            return 0
        for error in errors:
            print(f"[l9-control] {error}", file=sys.stderr)
        return 1

    if args.command == "verify-attestation":
        ok, errors = verify_attestation()
        if ok:
            print("[l9-control] runtime attestation verification passed")
            return 0
        for error in errors:
            print(f"[l9-control] {error}", file=sys.stderr)
        return 1

    if args.command == "select-gates":
        changed_files = _resolve_changed_files(args)
        selection = select_gates(changed_files)
        _print_json({"gate_ids": selection.ids, "commands": selection.commands})
        return 0

    if args.command == "review-signal":
        changed_files = _resolve_changed_files(args)
        ok, markdown = build_review_signal(changed_files)
        print(markdown)
        return 0 if ok else 1

    print("[l9-control] unknown command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
