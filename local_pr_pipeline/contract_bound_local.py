#!/usr/bin/env python3
"""Advisory contract-bound change check for PR Pipeline.

Referenced by .github/workflows/pr-pipeline.yml with `|| echo` (advisory).
Logic mirrors .github/workflows/l9-constitution-gate.yml contract-bound eval.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

CONTRACT_BOUND_PREFIXES = (
    "app/api/v1/",
    "app/agents/",
    "app/engines/",
    "app/services/",
    "chassis/",
)
CONTRACT_FILES_PREFIXES = (
    "docs/contracts/",
    "tests/contracts/",
    "scripts/verify_node_constitution.py",
)


def _diff_names(base: str, head: str) -> list[str]:
    output = subprocess.check_output(
        ["git", "diff", "--name-only", base, head],
        text=True,
    )
    return [line for line in output.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Advisory contract-bound gate")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()

    try:
        diff = _diff_names(args.base, args.head)
    except subprocess.CalledProcessError as exc:
        print(f"⚠️ Unable to compute diff ({exc}); treating as advisory pass")
        return 0

    touched_bound = [p for p in diff if p.startswith(CONTRACT_BOUND_PREFIXES)]
    touched_contracts = [p for p in diff if p.startswith(CONTRACT_FILES_PREFIXES)]

    print("### Contract-bound surfaces")
    if touched_bound:
        for path in touched_bound:
            print(f"- {path}")
    else:
        print("- none")

    print("### Contract/test surfaces")
    if touched_contracts:
        for path in touched_contracts:
            print(f"- {path}")
    else:
        print("- none")

    if touched_bound and not touched_contracts:
        print(
            "Contract-bound application files changed without corresponding "
            "docs/contracts, tests/contracts, or constitution verifier updates.",
            file=sys.stderr,
        )
        return 1

    print("✅ Contract-bound check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
