#!/usr/bin/env python3
"""Markdown link check for PR Pipeline.

Broader docs consistency lives in .github/workflows/docs-consistency.yml (SSOT).
This local helper performs a lightweight sanity check: markdown files under
scan roots must parse as text and contain no obviously broken `](/absolute)`
repo-root links when the target is missing. Relative-path link hygiene across
historical docs is deferred to the docs-consistency workflow / follow-up debt.

Exit 0 unless a hard parse/IO failure occurs.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ABS_LINK_RE = re.compile(r"\]\((/[A-Za-z0-9_./-]+)\)")
SCAN_GLOBS = ("*.md", "docs/**/*.md", "readme/**/*.md")


def _iter_markdown() -> list[Path]:
    files: set[Path] = set()
    for pattern in SCAN_GLOBS:
        files.update(REPO_ROOT.glob(pattern))
    return sorted(p for p in files if p.is_file())


def main() -> int:
    errors: list[str] = []
    checked = 0
    for path in _iter_markdown():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append(f"{path.relative_to(REPO_ROOT)}: unreadable — {exc}")
            continue
        for target in ABS_LINK_RE.findall(text):
            checked += 1
            candidate = REPO_ROOT / target.lstrip("/")
            if not candidate.exists():
                errors.append(f"{path.relative_to(REPO_ROOT)}: missing absolute link → {target}")

    if errors:
        for err in errors[:50]:
            print(f"❌ {err}")
        if len(errors) > 50:
            print(f"... and {len(errors) - 50} more")
        print(f"\nLink check failed: {len(errors)} broken absolute link(s)")
        return 1

    print(
        f"✅ Markdown link sanity OK — {checked} absolute link(s) checked "
        f"({len(_iter_markdown())} files). Relative-link debt deferred to docs-consistency.yml."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
