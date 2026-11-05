"""EIE-owned safe, duplicate-rejecting raw YAML admission.

L9_META:
  tier: 3
  domain: enrichment
  authority: native-domain-remediation-F-003
"""

from __future__ import annotations

from typing import Any, cast

import yaml
from yaml.events import AliasEvent
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

MAX_YAML_BYTES = 2 * 1024 * 1024
MAX_YAML_DEPTH = 64
MAX_YAML_NODES = 50_000
MAX_YAML_ALIASES = 128
MAX_YAML_EXPANSION = 100_000
_STRING_TAG = "tag:yaml.org,2002:str"
_MERGE_TAG = "tag:yaml.org,2002:merge"


class NativeYamlError(yaml.YAMLError):
    """Raw YAML cannot be interpreted unambiguously within native bounds."""


class NativeSafeLoader(yaml.SafeLoader):
    """Safe constructors with bounded composition and pre-construction checks."""

    def __init__(self, stream: bytes | str) -> None:
        super().__init__(stream)
        self._native_depth = 0
        self._native_nodes = 0
        self._native_aliases = 0

    def compose_node(self, parent: Any, index: Any) -> Node:
        self._native_nodes += 1
        if self._native_nodes > MAX_YAML_NODES:
            raise NativeYamlError("YAML node limit exceeded")
        if self.check_event(AliasEvent):
            self._native_aliases += 1
            if self._native_aliases > MAX_YAML_ALIASES:
                raise NativeYamlError("YAML alias limit exceeded")
        self._native_depth += 1
        try:
            if self._native_depth > MAX_YAML_DEPTH:
                raise NativeYamlError("YAML depth limit exceeded")
            return cast("Node", super().compose_node(parent, index))
        finally:
            self._native_depth -= 1

    def get_single_data(self) -> Any:
        """Validate the composed graph before SafeLoader constructs any data."""
        node = self.get_single_node()
        if node is None:
            return None
        self._validate_graph(node)
        return self.construct_document(node)

    @staticmethod
    def _validate_graph(root: Node) -> None:
        # Memoized size counts expanded occurrences, not only unique anchors.
        # Active-path detection refuses cycles before constructor recursion or
        # SafeConstructor merge flattening. Heights also bound aliased paths.
        active: set[int] = set()
        metrics: dict[int, tuple[int, int]] = {}

        def visit(node: Node, depth: int) -> tuple[int, int]:
            if depth > MAX_YAML_DEPTH:
                raise NativeYamlError("YAML expanded depth limit exceeded")
            identity = id(node)
            if identity in active:
                raise NativeYamlError("Cyclic YAML alias is forbidden")
            if identity in metrics:
                size, height = metrics[identity]
                if depth + height - 1 > MAX_YAML_DEPTH:
                    raise NativeYamlError("YAML expanded depth limit exceeded")
                return size, height
            active.add(identity)
            children: list[Node] = []
            if isinstance(node, MappingNode):
                keys: set[str] = set()
                merge_seen = False
                for key, value in node.value:
                    if key.tag == _MERGE_TAG:
                        if not isinstance(key, ScalarNode) or key.value != "<<":
                            raise NativeYamlError("YAML merge key must be the string '<<'")
                        if merge_seen:
                            raise NativeYamlError("Duplicate YAML merge key")
                        merge_seen = True
                        if not isinstance(value, MappingNode):
                            raise NativeYamlError("YAML merge requires a single mapping")
                    else:
                        if not isinstance(key, ScalarNode) or key.tag != _STRING_TAG:
                            raise NativeYamlError("YAML mapping keys must be strings")
                        if key.value in keys:
                            raise NativeYamlError(f"Duplicate explicit YAML key: {key.value!r}")
                        keys.add(key.value)
                    children.extend((key, value))
            elif isinstance(node, SequenceNode):
                children.extend(node.value)
            size, height = 1, 1
            for child in children:
                child_size, child_height = visit(child, depth + 1)
                size += child_size
                height = max(height, child_height + 1)
                if size > MAX_YAML_EXPANSION:
                    raise NativeYamlError("YAML alias expansion limit exceeded")
            active.remove(identity)
            metrics[identity] = size, height
            return size, height

        visit(root, 1)


def parse_native_yaml(content: bytes | str) -> Any:
    """Decode one bounded YAML document without rewriting its original bytes.

    Duplicate explicit keys and nonstring keys are refused recursively. One
    mapping-valued merge is allowed; explicit keys override inherited keys.
    Merge sequences, repeated merge keys, cycles and excessive expansion fail.
    Only SafeLoader's safe constructors are available.
    """
    if not isinstance(content, (bytes, str)):
        raise TypeError("Native YAML content must be bytes or text")
    size = len(content if isinstance(content, bytes) else content.encode("utf-8"))
    if size > MAX_YAML_BYTES:
        raise NativeYamlError("YAML byte limit exceeded")
    loader = NativeSafeLoader(content)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()
