#!/usr/bin/env python3
"""Terminology guard for PR Pipeline (port of compliance.yml Terminology Guard).

Fails when forbidden patterns appear under app/, engine/, or tests/:
  - print( in app/ or engine/
  - Optional[, List[, Dict[ in app/, engine/, or tests/
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PRINT_RE = re.compile(r"\bprint\(")
LEGACY_TYPING = (
    (re.compile(r"\bOptional\["), "Optional["),
    (re.compile(r"\bList\["), "List["),
    (re.compile(r"\bDict\["), "Dict["),
)


def _py_files(*roots: str) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        files.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    return sorted(files)


def main() -> int:
    violations = 0

    print_hits = [
        p
        for p in _py_files("app", "engine")
        if PRINT_RE.search(p.read_text(encoding="utf-8", errors="replace"))
    ]
    if print_hits:
        print("Found forbidden term: \\bprint\\(")
        for path in print_hits:
            print(path.relative_to(REPO_ROOT))
        violations += 1

    for pattern, label in LEGACY_TYPING:
        hits = [
            p
            for p in _py_files("app", "tests", "engine")
            if pattern.search(p.read_text(encoding="utf-8", errors="replace"))
        ]
        if hits:
            print(f"Found forbidden term: {label}")
            for path in hits:
                print(path.relative_to(REPO_ROOT))
            violations += 1

    if violations:
        print(f"\n❌ {violations} terminology violation(s) found")
        print("Use: list[T], T | None, structlog instead of print()")
        return 1

    print("✅ Terminology is consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
