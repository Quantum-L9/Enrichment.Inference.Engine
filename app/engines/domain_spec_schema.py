"""Native EIE spec checks; graph matching/ranking remains CEG-owned.

L9_META:
  tier: 3
  domain: enrichment
  authority: native-domain-supply-chain-g27r-v3
"""

from __future__ import annotations

import math
import re
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any

from app.engines.inference.rule_loader import RuleDefinition

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_TYPES = {
    "string",
    "float",
    "int",
    "integer",
    "bool",
    "boolean",
    "list",
    "dict",
    "object",
    "number",
    "date",
    "datetime",
}


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{context} must be a string-keyed mapping")
    return value


def _list(value: object, context: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{context} must be a {'nonempty ' if nonempty else ''}list")
    return value


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be a nonempty string")
    return value


def _identifier(value: object, context: str) -> str:
    text = _text(value, context)
    if not _IDENTIFIER.fullmatch(text):
        raise ValueError(f"{context} must be an identifier")
    return text


def _unique(value: str, seen: set[str], context: str) -> None:
    if value in seen:
        raise ValueError(f"Duplicate {context}: {value}")
    seen.add(value)


def _probability(value: object, context: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ValueError(f"{context} must be a finite number in [0, 1]")


def _integer(value: object, context: str, minimum: int, maximum: int | None = None) -> None:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(
            f"{context} must be an integer >= {minimum}"
            + (f" and <= {maximum}" if maximum is not None else "")
        )


def _field_refs(value: object, fields: set[str], context: str, *, nonempty: bool = False) -> None:
    seen: set[str] = set()
    for name in _list(value, context, nonempty=nonempty):
        name = _identifier(name, context)
        if name not in fields:
            raise ValueError(f"{context} references undeclared field {name}")
        _unique(name, seen, context)


def _reject_ceg(spec: dict[str, Any]) -> None:
    for key in (
        "gates",
        "ranking",
        "matchentities",
        "match_entities",
        "matching",
        "ranking_rules",
        "scoring_dimensions",
    ):
        if spec.get(key):
            raise ValueError(f"{key} is CEG-owned and cannot be authored by EIE")
    if "gates" in spec and spec["gates"] != []:
        raise ValueError("EIE gates placeholder must be []")
    if "scoring" in spec:
        scoring = _mapping(spec["scoring"], "scoring")
        if any(key != "dimensions" for key in scoring) or scoring.get("dimensions", []) != []:
            raise ValueError("EIE scoring may only contain empty dimensions")


def _properties(value: object, context: str) -> set[str]:
    fields: set[str] = set()
    for entry in _list(value, context, nonempty=True):
        prop = _mapping(entry, context)
        name = _identifier(prop.get("name"), f"{context}.name")
        _unique(name, fields, context)
        if not isinstance(prop.get("type"), str) or prop["type"] not in _TYPES:
            raise ValueError(f"Unsupported type for {context}.{name}")
        if "required" in prop and type(prop["required"]) is not bool:
            raise ValueError(f"{context}.{name}.required must be boolean")
    return fields


def _hints(value: object, fields: set[str]) -> None:
    hints = _mapping(value, "enrichment_hints")
    if not hints:
        return
    supported = {
        "objective_template",
        "priority_fields",
        "kb_context",
        "min_confidence",
        "refresh_interval_days",
        "max_variations_discovery",
        "max_variations_targeted",
    }
    if hints.keys() - supported:
        raise ValueError("Unsupported enrichment_hints keys")
    _text(hints.get("objective_template"), "enrichment_hints.objective_template")
    if "priority_fields" in hints:
        _field_refs(hints["priority_fields"], fields, "priority_fields")
    if "kb_context" in hints and hints["kb_context"] is not None:
        _text(hints["kb_context"], "kb_context")
    if "min_confidence" in hints:
        _probability(hints["min_confidence"], "min_confidence")
    for key in ("refresh_interval_days", "max_variations_discovery", "max_variations_targeted"):
        if key in hints:
            _integer(hints[key], key, 1)


def _rules(value: object, fields: set[str], tenant_id: str) -> None:
    seen: set[str] = set()
    for entry in _list(value, "inference_rules"):
        rule = _mapping(entry, "inference rule")
        _reject_ceg(rule)
        _unique(_text(rule.get("rule_id"), "rule_id"), seen, "rule_id")
        _text(rule.get("rule_or_model_version"), "rule_or_model_version")
        _probability(rule.get("confidence"), "rule confidence")
        _integer(rule.get("priority", 10), "rule priority", 0, 100)
        if rule.get("domain") is not None and rule["domain"] != tenant_id:
            raise ValueError("Rule domain must match the tenant")
        for key in ("conditions", "outputs"):
            for member in _list(rule.get(key), key, nonempty=True):
                member = _mapping(member, key)
                field = _identifier(member.get("field"), f"{key}.field")
                if field not in fields:
                    raise ValueError(f"{key} references undeclared field {field}")
        # Reuse the inference owner's operator/output semantics, without replacing
        # reviewed dictionaries with coerced/defaulted model output.
        RuleDefinition.model_validate(rule)


def _resolution(value: object, nodes: dict[str, set[str]]) -> None:
    registry = _mapping(value, "semantic_registry")
    if "enabled" in registry and type(registry["enabled"]) is not bool:
        raise ValueError("semantic_registry.enabled must be boolean")
    seen: set[str] = set()
    for entry in _list(registry.get("canonical_entities", []), "canonical_entities"):
        entity = _mapping(entry, "canonical entity")
        label = _identifier(entity.get("entity_type"), "entity_type")
        _unique(label, seen, "canonical entity")
        if label not in nodes:
            raise ValueError(f"Undeclared canonical entity {label}")
        if not isinstance(entity.get("resolution_strategy"), str) or entity[
            "resolution_strategy"
        ] not in {"exact", "fuzzy", "semantic", "hybrid"}:
            raise ValueError("Unsupported resolution_strategy")
        _probability(entity.get("resolution_threshold"), "resolution_threshold")
        _field_refs(entity.get("key_fields"), nodes[label], "key_fields", nonempty=True)


def _sync(value: object) -> None:
    sync = _mapping(value, "sync")
    seen: set[str] = set()
    for entry in _list(sync.get("endpoints"), "sync.endpoints", nonempty=True):
        endpoint = _mapping(entry, "sync endpoint")
        path = _text(endpoint.get("path"), "sync.path")
        if not re.fullmatch(r"/v1/sync/[A-Za-z0-9_-]+", path):
            raise ValueError("sync.path must be a local /v1/sync/<endpoint> path")
        _unique(path, seen, "sync endpoint path")
        _identifier(endpoint.get("targetnode"), "sync.targetnode")
        _identifier(endpoint.get("idproperty"), "sync.idproperty")
        if "batchstrategy" in endpoint and endpoint["batchstrategy"] != "UNWINDMERGE":
            raise ValueError("Unsupported sync.batchstrategy")
        # CEG owns target/id vocabulary; not all targets exist in EIE ontology.


def validate_spec(raw: object, tenant_id: str) -> dict[str, Any]:
    """Validate EIE-owned native structure, preserving all reviewed values."""
    spec = _mapping(raw, "native spec")
    if spec.get("name") != tenant_id:
        raise ValueError("Native spec name must match its tenant id")
    version = _text(spec.get("version"), "version")
    if not _SEMVER.fullmatch(version):
        raise ValueError("Native spec version must be a semantic-version string")
    prerelease = version.split("+", 1)[0].partition("-")[2]
    if any(
        part.isdigit() and len(part) > 1 and part.startswith("0") for part in prerelease.split(".")
    ):
        raise ValueError("Numeric version prerelease identifiers must not have leading zeroes")
    if "domain" in spec or "domain_identity" in spec:
        raise ValueError("Native identity is top-level name/version, not legacy identity blocks")
    _reject_ceg(spec)
    ontology = _mapping(spec.get("ontology"), "ontology")
    nodes: dict[str, set[str]] = {}
    for entry in _list(ontology.get("nodes"), "ontology.nodes", nonempty=True):
        node = _mapping(entry, "ontology node")
        label = _identifier(node.get("label"), "node.label")
        if label in nodes:
            raise ValueError(f"Duplicate node label: {label}")
        _reject_ceg(node)
        nodes[label] = _properties(node.get("properties"), f"{label}.properties")
        if "enrichment_hints" in node:
            _hints(node["enrichment_hints"], nodes[label])
    if "queryschema" in spec:
        query = _mapping(spec["queryschema"], "queryschema")
        _properties(query.get("fields"), "queryschema.fields")
    if "inference_rules" in spec:
        _rules(spec["inference_rules"], set().union(*nodes.values()), tenant_id)
    if "inference_rules_path" in spec:
        path = _text(spec["inference_rules_path"], "inference_rules_path")
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", path)
            or any(part in {".", ".."} for part in path.split("/"))
            or PurePosixPath(path).suffix not in {".yaml", ".yml"}
        ):
            raise ValueError("inference_rules_path must be a bounded relative YAML path")
        if spec.get("inference_rules"):
            raise ValueError("Declare inline inference_rules or a reference, not both")
    if "semantic_registry" in spec:
        _resolution(spec["semantic_registry"], nodes)
    if "sync" in spec:
        _sync(spec["sync"])
    return deepcopy(spec)
