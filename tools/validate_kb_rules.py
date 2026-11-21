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


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping with two equal explicit keys.

    PyYAML keeps the last of duplicate keys silently, so a file could hide an
    invalid value behind a valid one. YAML requires unique keys; merge keys
    (``<<``) may still be overridden by an explicit key, as the spec allows.
    """


def _construct_unique_mapping(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    seen: set[object] = set()
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError:
            continue  # SafeLoader reports an unhashable key itself
        if key in seen:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _load(text: str) -> object:
    loader = _UniqueKeyLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def kb_rule_errors(kb_dir: Path) -> list[str]:
    """Return one message per violation under *kb_dir*; empty when it conforms."""
    if not kb_dir.exists():
        return []
    errors: list[str] = []
    for path in sorted({*kb_dir.rglob("*.yaml"), *kb_dir.rglob("*.yml")}):
        try:
            data = _load(path.read_text(encoding="utf-8"))
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
