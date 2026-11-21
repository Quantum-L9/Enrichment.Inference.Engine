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


def workflow_errors(directory: Path) -> list[str]:
    """Return one message per workflow file under *directory* that is not valid."""
    files = sorted({*directory.glob("*.yml"), *directory.glob("*.yaml")})
    if not files:
        return [f"{directory}: no workflow files found"]
    errors: list[str] = []
    for path in files:
        try:
            data = _load(path.read_text(encoding="utf-8"))
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
