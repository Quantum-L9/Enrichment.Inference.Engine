#!/usr/bin/env python3
# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: enrichment
# layer: [ci, governance]
# tags: [L9_TEMPLATE, ci, l9, select-gates]
# owner: platform
# status: active
# --- /L9_META ---
"""Execute the change-selected L9 gates.

Referenced by .github/workflows/pr-pipeline.yml ("Select and run gates" step in
the l9 job). The step first runs::

    python scripts/l9_contract_control.py select-gates --base ... --head ... > gates.json

which emits JSON of the form::

    {"gate_ids": ["api-surface", ...], "commands": ["pytest ...", ...]}

This script reads that file and runs each selected command in order. It runs
ALL commands (fail-open reporting) and exits non-zero if any command failed, so
one run surfaces every failing selected gate.

The gates file must live inside the repository, and every command in it must be
declared in docs/contracts/enforcement/ci-gate.yaml; anything else exits 2 before
any command runs.

Usage:
    python local_pr_pipeline/run_pr_select_gates.py gates.json
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.l9_contract_control import declared_gate_commands  # noqa: E402


def _confined_gates_path(raw: str) -> Path:
    """Resolve the gates file and refuse any location outside the repository root."""
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(REPO_ROOT):
        print(f"❌ gates file must be inside the repository: {raw}")
        raise SystemExit(2)
    return resolved


def _undeclared_commands(commands: list[str]) -> list[str]:
    """Commands not declared in docs/contracts/enforcement/ci-gate.yaml."""
    declared = declared_gate_commands()
    return [command for command in commands if command not in declared]


def _load_gates(path: Path) -> tuple[list[str], list[str]]:
    if not path.exists():
        print(f"❌ gates file not found: {path}")
        raise SystemExit(2)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"❌ gates file is not valid JSON ({path}): {exc}")
        raise SystemExit(2) from exc

    gate_ids = data.get("gate_ids", []) if isinstance(data, dict) else []
    commands = data.get("commands", []) if isinstance(data, dict) else []
    if not isinstance(gate_ids, list) or not isinstance(commands, list):
        print("❌ gates file must contain list 'gate_ids' and 'commands'")
        raise SystemExit(2)
    return [str(g) for g in gate_ids], [str(c) for c in commands]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run change-selected L9 gates")
    parser.add_argument("gates_file", help="Path to gates.json from select-gates")
    args = parser.parse_args()

    gate_ids, commands = _load_gates(_confined_gates_path(args.gates_file))
    undeclared = _undeclared_commands(commands)
    if undeclared:
        print("❌ gates file lists commands not declared in the CI gate contract:")
        for command in undeclared:
            print(f"  - {command}")
        return 2

    print(f"Selected gates: {gate_ids or '(none)'}")
    if not commands:
        print("✅ No gates selected for the changed files — nothing to run")
        return 0

    failures: list[str] = []
    for command in commands:
        print(f"\n▶ {command}")
        result = subprocess.run(shlex.split(command), cwd=REPO_ROOT)
        if result.returncode != 0:
            failures.append(command)
            print(f"❌ gate command failed (exit {result.returncode}): {command}")

    print("")
    if failures:
        print(f"❌ {len(failures)}/{len(commands)} selected gate command(s) failed")
        for command in failures:
            print(f"  - {command}")
        return 1

    print(f"✅ All {len(commands)} selected gate command(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
