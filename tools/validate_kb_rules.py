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
    # Keys are compared by the value safe_load gives them, not by spelling:
    # `true`/`True` and `1`/`0x1` collapse to one dict key, hiding a value.
    constructor = yaml.SafeLoader("")
    errors: list[str] = []
    seen_nodes: set[int] = set()
    stack: list[yaml.Node] = [root] if root is not None else []
    while stack:
        node = stack.pop()
        if id(node) in seen_nodes:
            continue
        seen_nodes.add(id(node))
        if isinstance(node, yaml.MappingNode):
            keys: set[object] = set()
            for key_node, value_node in node.value:
                stack.extend((key_node, value_node))
                if key_node.tag == "tag:yaml.org,2002:merge" or not isinstance(
                    key_node, yaml.ScalarNode
                ):
                    continue
                key = constructor.construct_object(key_node, deep=True)
                if key in keys:
                    mark = key_node.start_mark
                    errors.append(f"line {mark.line + 1}: found duplicate key {key_node.value!r}")
                keys.add(key)
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)
    constructor.dispose()
    return errors


def kb_rule_errors(kb_dir: Path) -> list[str]:
    """Return one message per violation under *kb_dir*; empty when it conforms."""
    if not kb_dir.exists():
        return []
    errors: list[str] = []
    for path in sorted({*kb_dir.rglob("*.yaml"), *kb_dir.rglob("*.yml")}):
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
