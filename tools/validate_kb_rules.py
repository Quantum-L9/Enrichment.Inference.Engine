"""KB rule schema gate (repo-validate leaf; EIE org-CI cutover, Phase 1 row 20).

Every YAML file under kb/ must parse to a mapping, and each entry of a
top-level `rules` list must name its `field` and carry a `conditions` or
`when` block. This is the check `.github/workflows/compliance.yml` ran inline
("KB YAML Schema Validation"); as a `make validate` leaf it runs in the
required `Analyze (central Core)` check too. No kb/ directory passes.

Exit 0 when every file conforms, 1 with one line per violation otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


def kb_rule_errors(kb_dir: Path) -> list[str]:
    """Return one message per violation under *kb_dir*; empty when it conforms."""
    if not kb_dir.exists():
        return []
    errors: list[str] = []
    for path in sorted({*kb_dir.rglob("*.yaml"), *kb_dir.rglob("*.yml")}):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            errors.append(f"{path}: {exc}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{path}: Root must be a YAML mapping")
            continue
        rules = data.get("rules")
        if rules is None:
            continue
        if not isinstance(rules, list):
            errors.append(f"{path}: `rules` must be a list")
            continue
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                errors.append(f"{path}: Rule {index} must be a mapping")
                continue
            if "field" not in rule:
                errors.append(f"{path}: Rule {index} missing required key: field")
            if "conditions" not in rule and "when" not in rule:
                errors.append(f"{path}: Rule {index} missing conditions/when block")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("kb_dir", nargs="?", default="kb", type=Path)
    args = parser.parse_args(argv)
    errors = kb_rule_errors(args.kb_dir)
    for error in errors:
        print(f"  {error}")
    if errors:
        print(f"{len(errors)} KB validation error(s)")
        return 1
    print(f"KB rule schema: PASS ({args.kb_dir})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
