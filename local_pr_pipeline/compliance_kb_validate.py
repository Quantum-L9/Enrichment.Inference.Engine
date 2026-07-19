#!/usr/bin/env python3
"""Validate knowledge-base YAML files.

Referenced by .github/workflows/pr-pipeline.yml ("Validate KB YAML" and
"KB YAML Schema Validation" steps). The script was missing from the repo, which
made the `validate` gate (and the downstream CI gate) fail on every PR — a gate
defect unrelated to any code change. This restores it.

Checks performed against every `kb/**/*.yaml` and `kb/**/*.yml` file:
  1. The file parses as valid YAML.
  2. The document is non-empty (parses to something other than null).

Exit code 0 when all files pass, 1 otherwise. No third-party schema validation
is performed here; structural/semantic schema enforcement lives in the contract
test suite.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
KB_DIR = REPO_ROOT / "kb"


def _kb_files() -> list[Path]:
    if not KB_DIR.is_dir():
        return []
    return sorted(p for p in KB_DIR.rglob("*") if p.suffix in {".yaml", ".yml"})


def main() -> int:
    files = _kb_files()
    if not KB_DIR.is_dir():
        print(f"✅ No kb/ directory at {KB_DIR.relative_to(REPO_ROOT)} — nothing to validate")
        return 0
    if not files:
        print("✅ kb/ directory present but contains no YAML files — nothing to validate")
        return 0

    errors: list[str] = []
    for path in files:
        rel = path.relative_to(REPO_ROOT)
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            errors.append(f"{rel}: invalid YAML — {exc}")
            continue
        if doc is None:
            errors.append(f"{rel}: empty document")

    if errors:
        for err in errors:
            print(f"❌ {err}")
        print(f"\nKB validation failed: {len(errors)} error(s) across {len(files)} file(s)")
        return 1

    print(f"✅ KB YAML valid — {len(files)} file(s) checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
