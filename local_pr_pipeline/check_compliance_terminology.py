#!/usr/bin/env python3
# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: enrichment
# layer: [ci, governance, compliance]
# tags: [L9_TEMPLATE, ci, compliance, terminology, chassis-isolation]
# owner: platform
# status: active
# --- /L9_META ---
"""Terminology / deprecated-transport guard.

Referenced by .github/workflows/pr-pipeline.yml ("Terminology Guard" step in the
compliance job). The script was missing from the repo, so the compliance gate
failed on every PR regardless of the change — a gate defect unrelated to any
code change. This restores it.

What it enforces (aligned with AGENTS.md contracts C-21 / ARCH-003 and the
CLAUDE.md "Transport Precision" section):

  Production code under app/ must NOT import or dispatch through the deprecated
  compatibility artifacts `chassis/envelope.py`, `chassis/router.py`, or
  `chassis/registry.py`. These remain in the repo for migration/history only;
  reintroducing them into production dispatch is a CRITICAL contract violation.

Only real Python `import` statements are inspected (via AST), so prose/comments
that merely *mention* the deprecated modules (as this repo's governance docs do)
are never flagged. Exit 0 when clean, 1 on any violation.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"

# Deprecated dispatch modules that must never be imported by production code.
BANNED_IMPORT_ROOTS = {
    "chassis.envelope",
    "chassis.router",
    "chassis.registry",
}


def _iter_app_py_files() -> list[Path]:
    if not APP_DIR.is_dir():
        return []
    return sorted(APP_DIR.rglob("*.py"))


def _module_is_banned(module: str | None) -> bool:
    if not module:
        return False
    return any(module == root or module.startswith(root + ".") for root in BANNED_IMPORT_ROOTS)


def _scan_file(path: Path) -> list[str]:
    findings: list[str] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return [f"{path.relative_to(REPO_ROOT)}: could not parse ({exc})"]

    rel = path.relative_to(REPO_ROOT)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _module_is_banned(node.module):
            findings.append(
                f"{rel}:{node.lineno}: imports deprecated dispatch module '{node.module}' "
                f"(C-21/ARCH-003: never reintroduce deprecated chassis dispatch in production)"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _module_is_banned(alias.name):
                    findings.append(
                        f"{rel}:{node.lineno}: imports deprecated dispatch module "
                        f"'{alias.name}' (C-21/ARCH-003)"
                    )
    return findings


def main() -> int:
    if not APP_DIR.is_dir():
        print(f"✅ No app/ directory at {APP_DIR} — nothing to check")
        return 0

    files = _iter_app_py_files()
    violations: list[str] = []
    for path in files:
        violations.extend(_scan_file(path))

    if violations:
        print("❌ Terminology / deprecated-transport guard failed:")
        for item in violations:
            print(f"  {item}")
        print(
            f"\n{len(violations)} violation(s) across {len(files)} file(s). "
            "Remove production reliance on deprecated chassis dispatch."
        )
        return 1

    print(
        f"✅ Terminology guard passed — {len(files)} app/ file(s) checked, no deprecated dispatch"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
