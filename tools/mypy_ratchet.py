#!/usr/bin/env python3
"""mypy ratchet for `make agent-check` stage [3/8] (ADR-001 / WAIVER-001).

ADR-001 makes mypy non-blocking during the annotation rollout, but a gate that
ignores type errors entirely lets new ones accumulate. This ratchet fails on any
mypy error that is not recorded in ``tools/mypy_baseline.txt`` and reports
recorded errors that no longer occur so the ledger can shrink.

Fingerprints are ``<path>: <message>`` without line numbers, so unrelated edits
that move code do not churn the ledger, and fixing one recorded error does not
license a different one in its place.

Usage:
    python tools/mypy_ratchet.py              # check
    python tools/mypy_ratchet.py --update     # rewrite the ledger to the current errors
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE = REPO_ROOT / "tools" / "mypy_baseline.txt"
_ERROR_RE = re.compile(r"^(?P<path>[^:\s]+\.py):\d+(?::\d+)?: error: (?P<message>.+)$")


def fingerprints(mypy_output: str) -> Counter[str]:
    found: Counter[str] = Counter()
    for line in mypy_output.splitlines():
        match = _ERROR_RE.match(line.strip())
        if match:
            found[f"{match['path']}: {match['message'].strip()}"] += 1
    return found


def load_baseline(path: Path = BASELINE) -> Counter[str]:
    if not path.exists():
        return Counter()
    entries = [
        line.rstrip("\n")
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    return Counter(entries)


def compare(current: Counter[str], baseline: Counter[str]) -> tuple[list[str], list[str]]:
    new = sorted((current - baseline).elements())
    fixed = sorted((baseline - current).elements())
    return new, fixed


def run_mypy() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "app"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="rewrite the ledger")
    args = parser.parse_args(argv)

    returncode, output = run_mypy()
    current = fingerprints(output)
    if returncode not in (0, 1) or (returncode == 1 and not current):
        # mypy itself failed (crash, bad config): never read that as "no errors".
        print(output)
        print("mypy-ratchet: FAIL — mypy did not produce a usable result")
        return 2

    if args.update:
        header = (
            "# mypy ratchet ledger (ADR-001 / WAIVER-001). Shrink-only: remove lines as\n"
            "# errors are fixed; never add one to make a new error pass.\n"
        )
        BASELINE.write_text(header + "".join(f"{fp}\n" for fp in sorted(current.elements())))
        print(f"mypy-ratchet: ledger rewritten with {sum(current.values())} error(s)")
        return 0

    new, fixed = compare(current, load_baseline())
    for fp in fixed:
        print(f"mypy-ratchet: fixed (remove from tools/mypy_baseline.txt): {fp}")
    if new:
        print(output)
        for fp in new:
            print(f"mypy-ratchet: NEW error not in the ledger: {fp}")
        print(f"mypy-ratchet: FAIL — {len(new)} new mypy error(s)")
        return 1
    print(
        f"mypy-ratchet: PASS — {sum(current.values())} recorded error(s), 0 new"
        + (f", {len(fixed)} fixed" if fixed else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
