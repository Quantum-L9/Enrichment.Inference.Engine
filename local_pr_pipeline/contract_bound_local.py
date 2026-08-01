#!/usr/bin/env python3
# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: enrichment
# layer: [ci, governance]
# tags: [L9_TEMPLATE, ci, l9, contract-bound]
# owner: platform
# status: active
# --- /L9_META ---
"""Contract-bound change gate (local/CI).

Referenced by .github/workflows/pr-pipeline.yml ("Contract-bound change gate"
step in the l9 job), invoked as::

    python local_pr_pipeline/contract_bound_local.py --base <sha> --head <sha>

The step is advisory in the workflow (`|| echo ...`), but the script itself
reports precisely whether contract-bound files changed without the required
companion contract/test/control updates, per
docs/contracts/enforcement/review-policy.yaml.

This is a thin CLI wrapper around `scripts.l9_contract_control.build_review_signal`,
which is the single source of truth for the contract-bound policy. Exit 0 when
the review signal passes (or no contract-bound files changed), 1 otherwise.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.l9_contract_control import build_review_signal  # noqa: E402


def _git_changed_files(base: str, head: str) -> list[str]:
    output = subprocess.check_output(
        ["git", "diff", "--name-only", base, head],
        cwd=REPO_ROOT,
        text=True,
    )
    return [line.strip() for line in output.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Contract-bound change gate")
    parser.add_argument("--base", type=str, help="Base git ref")
    parser.add_argument("--head", type=str, help="Head git ref")
    parser.add_argument("--files", nargs="*", default=[], help="Explicit changed files")
    args = parser.parse_args()

    if args.files:
        changed = [f for f in args.files if f]
    elif args.base and args.head:
        changed = _git_changed_files(args.base, args.head)
    else:
        print("⚠️  No --files and no --base/--head provided; nothing to evaluate")
        return 0

    ok, markdown = build_review_signal(changed)
    print(markdown)
    print("")
    if ok:
        print("✅ Contract-bound change gate passed")
        return 0
    print("❌ Contract-bound change gate found blocking findings")
    return 1


if __name__ == "__main__":
    sys.exit(main())
