#!/usr/bin/env python3
"""Emit pytest --deselect args for entries in .l9/baselines/test-quarantine.yml."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


def _resolve_ledger(raw: str, root: Path) -> Path:
    candidate = Path(raw).expanduser()
    resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        print(f"ERROR: ledger path escapes repository root: {resolved}", file=sys.stderr)
        raise SystemExit(2) from None
    if not resolved.is_file():
        print(f"ERROR: ledger file not found: {resolved}", file=sys.stderr)
        raise SystemExit(2)
    return resolved


def main() -> int:
    root = Path.cwd().resolve()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ledger",
        default=".l9/baselines/test-quarantine.yml",
        help="Path to quarantine ledger YAML (must stay under repo root)",
    )
    args = parser.parse_args()
    ledger = _resolve_ledger(args.ledger, root)
    data = yaml.safe_load(ledger.read_text(encoding="utf-8")) or {}
    entries = data.get("entries", [])
    node_ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        node_id = entry.get("test_node_id")
        if isinstance(node_id, str) and node_id.strip():
            node_ids.append(node_id.strip())
    for node_id in sorted(set(node_ids)):
        print(f"--deselect={node_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
