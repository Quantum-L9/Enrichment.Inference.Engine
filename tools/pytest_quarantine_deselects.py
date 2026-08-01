#!/usr/bin/env python3
"""Emit pytest --deselect args for entries in .l9/baselines/test-quarantine.yml."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ledger",
        default=".l9/baselines/test-quarantine.yml",
        help="Path to quarantine ledger YAML",
    )
    args = parser.parse_args()
    data = yaml.safe_load(Path(args.ledger).read_text(encoding="utf-8")) or {}
    entries = data.get("entries", [])
    node_ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        node_id = entry.get("test_node_id")
        if isinstance(node_id, str) and node_id.strip():
            node_ids.append(node_id.strip())
    # Stable unique order
    for node_id in sorted(set(node_ids)):
        print(f"--deselect={node_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
