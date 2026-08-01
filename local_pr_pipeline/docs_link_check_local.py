#!/usr/bin/env python3
# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: enrichment
# layer: [ci, governance, docs]
# tags: [L9_TEMPLATE, ci, docs, link-check]
# owner: platform
# status: active
# --- /L9_META ---
"""Markdown relative-link checker.

Referenced by .github/workflows/pr-pipeline.yml ("Run markdown link check" step
in the docs job). The script was missing from the repo, so the docs gate failed
on every PR regardless of the change. This restores it.

Behavior:
  - By default, checks the repo's top-level (root) governance markdown files.
    Pass explicit paths to check a different set, or `--all` to walk the whole
    repository tree.
  - Only *relative* links are validated (targets that resolve to a path on
    disk). External links (http/https/mailto/tel), in-page anchors (`#...`),
    and empty targets are ignored. A trailing `#anchor` on a relative link is
    stripped before resolution.

Exit 0 when all checked links resolve, 1 otherwise.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "tel:", "//")


def _default_targets() -> list[Path]:
    """Root-level markdown files (the canonical governance doc set)."""
    return sorted(REPO_ROOT.glob("*.md"))


def _resolve_targets(args_paths: list[str], scan_all: bool) -> list[Path]:
    if scan_all:
        return sorted(p for p in REPO_ROOT.rglob("*.md") if ".git" not in p.parts)
    if args_paths:
        return [Path(p) if Path(p).is_absolute() else REPO_ROOT / p for p in args_paths]
    return _default_targets()


def _check_file(md_path: Path) -> list[str]:
    broken: list[str] = []
    text = md_path.read_text(encoding="utf-8", errors="ignore")
    for target in LINK_RE.findall(text):
        raw = target.strip()
        if not raw or raw.startswith(EXTERNAL_PREFIXES) or raw.startswith("#"):
            continue
        path_part = raw.split("#", 1)[0].strip()
        if not path_part:
            continue
        resolved = md_path.parent / path_part
        if not resolved.exists():
            try:
                rel = md_path.relative_to(REPO_ROOT)
            except ValueError:
                rel = md_path
            broken.append(f"{rel} -> {raw}")
    return broken


def main() -> int:
    parser = argparse.ArgumentParser(description="Markdown relative-link checker")
    parser.add_argument("paths", nargs="*", help="Markdown files to check (default: root *.md)")
    parser.add_argument("--all", action="store_true", help="Walk the whole repo tree")
    args = parser.parse_args()

    targets = [p for p in _resolve_targets(args.paths, args.all) if p.is_file()]
    if not targets:
        print("✅ No markdown files to check")
        return 0

    broken: list[str] = []
    for md_path in targets:
        broken.extend(_check_file(md_path))

    if broken:
        print("❌ Broken relative markdown links:")
        for item in broken:
            print(f"  {item}")
        print(f"\n{len(broken)} broken link(s) across {len(targets)} file(s)")
        return 1

    print(f"✅ Markdown links OK — {len(targets)} file(s) checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
