#!/usr/bin/env python3
"""Enforce the zero-debt MyPy ledger used by the strict type gate.

``make type-check`` runs MyPy directly before this guard, so every MyPy finding
fails the target. This legacy ledger remains only to prevent recorded findings
from being reintroduced as an exception to that rule.

Usage:
    python tools/mypy_ratchet.py
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE = REPO_ROOT / "tools" / "mypy_baseline.txt"


def recorded_entries(path: Path = BASELINE) -> list[str]:
    """Return non-comment entries from the retired MyPy debt ledger."""
    if not path.exists():
        return []
    return [
        line.rstrip("\n")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def main() -> int:
    """Fail if any MyPy finding is recorded as allowable debt."""
    entries = recorded_entries()
    if entries:
        print("mypy-ratchet: FAIL — strict MyPy permits no recorded debt")
        for entry in entries:
            print(f"mypy-ratchet: remove from tools/mypy_baseline.txt: {entry}")
        return 1

    print("mypy-ratchet: PASS — zero recorded MyPy errors; direct MyPy is strict")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
