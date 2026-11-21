"""Workflow YAML parse gate (repo-validate leaf; EIE org-CI cutover, Phase 1 row 18).

Every `.github/workflows/*.yml` / `*.yaml` file must parse as YAML to a
mapping with a `jobs` mapping. A workflow that does not parse never runs, and
GitHub reports that on no pull-request check, so a broken file could merge
while the required `Analyze (central Core)` stayed green. ci.yml and
pr-pipeline.yml ran this parse; as a `make validate` leaf it runs in the
required check until Core's consumer workflow-integrity lint (harvest row C13)
supersedes it.

Exit 0 when every workflow parses, 1 with one line per broken file otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


def workflow_errors(directory: Path) -> list[str]:
    """Return one message per workflow file under *directory* that is not valid."""
    files = sorted({*directory.glob("*.yml"), *directory.glob("*.yaml")})
    if not files:
        return [f"{directory}: no workflow files found"]
    errors: list[str] = []
    for path in files:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            errors.append(f"{path}: {exc}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{path}: root must be a YAML mapping")
        elif not isinstance(data.get("jobs"), dict):
            errors.append(f"{path}: no `jobs` mapping")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", nargs="?", default=".github/workflows", type=Path)
    args = parser.parse_args(argv)
    errors = workflow_errors(args.directory)
    for error in errors:
        print(f"  {error}")
    if errors:
        print(f"{len(errors)} workflow file error(s)")
        return 1
    print(f"Workflow YAML: PASS ({args.directory})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
