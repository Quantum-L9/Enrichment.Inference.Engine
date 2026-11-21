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


def _duplicate_key_errors(text: str) -> list[str]:
    """Return one message per mapping that repeats an explicit key.

    PyYAML keeps the last of duplicate keys silently, so a file could hide an
    invalid value behind a valid one. The check runs on the composed node
    graph, before anything is constructed: every mapping node is visited once
    (aliases included, merge operands included) and only its own explicit keys
    are compared, so a merge key (``<<``) overridden by an explicit key stays
    legal and in-place merge flattening cannot produce a false duplicate.
    Construction itself stays ``yaml.safe_load`` (AGENTS.md C-08).
    """
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    errors: list[str] = []
    seen_nodes: set[int] = set()
    stack: list[yaml.Node] = [root] if root is not None else []
    while stack:
        node = stack.pop()
        if id(node) in seen_nodes:
            continue
        seen_nodes.add(id(node))
        if isinstance(node, yaml.MappingNode):
            keys: set[tuple[str, str]] = set()
            for key_node, value_node in node.value:
                stack.extend((key_node, value_node))
                if key_node.tag == "tag:yaml.org,2002:merge" or not isinstance(
                    key_node, yaml.ScalarNode
                ):
                    continue
                key = (key_node.tag, key_node.value)
                if key in keys:
                    mark = key_node.start_mark
                    errors.append(f"line {mark.line + 1}: found duplicate key {key_node.value!r}")
                keys.add(key)
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)
    return errors


def workflow_errors(directory: Path) -> list[str]:
    """Return one message per workflow file under *directory* that is not valid."""
    files = sorted({*directory.glob("*.yml"), *directory.glob("*.yaml")})
    if not files:
        return [f"{directory}: no workflow files found"]
    errors: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        try:
            duplicates = _duplicate_key_errors(text)
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            errors.append(f"{path}: {exc}")
            continue
        if duplicates:
            errors.extend(f"{path}: {message}" for message in duplicates)
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
