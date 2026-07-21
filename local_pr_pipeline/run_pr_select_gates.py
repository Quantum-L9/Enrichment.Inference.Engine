#!/usr/bin/env python3
"""Run gate commands selected by scripts/l9_contract_control.py select-gates.

Referenced by .github/workflows/pr-pipeline.yml. Mirrors the inline runner in
.github/workflows/l9-contract-control.yml ("Run selected commands").
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: run_pr_select_gates.py <gates.json>", file=sys.stderr)
        return 2

    gates_path = Path(argv[1])
    if not gates_path.is_file():
        print(f"[l9-control] missing gates file: {gates_path}", file=sys.stderr)
        return 1

    payload = json.loads(gates_path.read_text(encoding="utf-8"))
    commands = payload.get("commands", [])
    if not commands:
        print("[l9-control] no matching gates selected")
        return 0

    seen: set[str] = set()
    ordered: list[str] = []
    for command in commands:
        if command not in seen:
            seen.add(command)
            ordered.append(command)

    for command in ordered:
        print(f"[l9-control] running: {command}")
        completed = subprocess.run(command, shell=True, check=False)
        if completed.returncode != 0:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
