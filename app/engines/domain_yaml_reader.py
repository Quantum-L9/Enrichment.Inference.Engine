"""
Domain YAML Reader — Reads DomainSpec → target_schema + enrichment_hints.

Reads the same YAML format that the graph engine's DomainPackLoader consumes.
Extracts:
  - ontology.nodes[].properties → target_schema dict for EnrichRequest
  - ontology.nodes[].enrichment_hints → per-node-type enrichment config
  - inference rules referenced in the spec

The graph engine ignores enrichment_hints (not in its Pydantic model).
This reader uses them to auto-configure enrichment behavior per node type.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger("domain_yaml_reader")

# Identity contract (operator decision 2026-10-22): tenant id == domain spec id
# == Neo4j database name. One customer is one directory `domains/<tenant>/spec.yaml`,
# one versioned spec and one database, so a tenant id has to be a legal Neo4j
# database name: lowercase, starting with a letter, then letters, digits, dots
# or dashes, 3-63 characters, and never starting with the reserved `system`
# prefix. Vertical templates are provenance only (`metadata.template.*`); there
# is no tenant-to-domain mapping layer anywhere.
#
# This is the ONE grammar. The reader applies it to every id that reaches the
# filesystem (ids arrive from request payloads: `domain_id` / `object_type`,
# so "..", "/" and any other escape from the domains root are refused by the
# same check), and the API-side registry (app/api/v1/converge.py) imports it
# rather than keeping a second regex that could drift.
TENANT_ID_PATTERN: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9.-]{2,62}$")
TENANT_ID_RESERVED_PREFIX = "system"


def is_legal_tenant_id(value: object) -> bool:
    """Whether ``value`` can name a tenant, its domain spec and its Neo4j database.

    The lowercase check runs before the pattern so a mixed-case id is refused
    outright rather than silently lowercased into a different identity.
    """
    if not isinstance(value, str) or value != value.lower():
        return False
    if value.startswith(TENANT_ID_RESERVED_PREFIX):
        return False
    return TENANT_ID_PATTERN.fullmatch(value) is not None


@dataclass
class EnrichmentHints:
    """Per-node-type enrichment configuration from domain YAML."""

    objective_template: str = ""
    priority_fields: list[str] = field(default_factory=list)
    kb_context: str | None = None
    refresh_interval_days: int = 90
    min_confidence: float = 0.75
    max_variations_discovery: int = 5
    max_variations_targeted: int = 3


@dataclass
class NodeSchema:
    """Schema for a single node type extracted from domain YAML."""

    label: str
    properties: dict[str, str]  # field_name → type_string
    required_fields: list[str] = field(default_factory=list)
    enrichment_hints: EnrichmentHints | None = None


@dataclass
class DomainEnrichmentConfig:
    """Complete enrichment configuration extracted from a domain YAML."""

    domain_id: str
    version: str
    node_schemas: dict[str, NodeSchema]  # label → NodeSchema
    inference_rules_path: str | None = None
    raw_spec: dict[str, Any] = field(default_factory=dict)


class DomainYamlReader:
    """
    Reads domain spec YAML files and extracts enrichment-relevant config.
    Compatible with the graph engine's DomainPackLoader format.
    """

    def __init__(self, domains_root: str | Path = "domains"):
        self._root = Path(domains_root)
        self._cache: dict[str, DomainEnrichmentConfig] = {}

    def load(self, domain_id: str) -> DomainEnrichmentConfig:
        if domain_id in self._cache:
            return self._cache[domain_id]
        if not is_legal_tenant_id(domain_id):
            raise ValueError(f"Invalid domain id: {domain_id!r}")

        spec_path = self._root / domain_id / "spec.yaml"
        if not spec_path.exists():
            spec_path = self._root / f"{domain_id}.yaml"
        if not spec_path.exists():
            raise FileNotFoundError(f"Domain spec not found: {spec_path}")

        with open(spec_path) as f:
            raw = yaml.safe_load(f)

        config = self._parse(domain_id, raw)
        self._cache[domain_id] = config
        logger.info(
            "domain_loaded",
            domain=domain_id,
            nodes=len(config.node_schemas),
            version=config.version,
        )
        return config

    def get_target_schema(self, domain_id: str, node_label: str) -> dict[str, str]:
        """Extract target_schema dict for EnrichRequest from a node type."""
        config = self.load(domain_id)
        node = config.node_schemas.get(node_label)
        if not node:
            raise KeyError(f"Node label '{node_label}' not found in domain '{domain_id}'")
        return node.properties

    def get_enrichment_hints(self, domain_id: str, node_label: str) -> dict[str, Any]:
        """Extract enrichment_hints as a plain dict for MetaPromptPlanner."""
        config = self.load(domain_id)
        node = config.node_schemas.get(node_label)
        if not node or not node.enrichment_hints:
            return {}
        hints = node.enrichment_hints
        return {
            "objective_template": hints.objective_template,
            "priority_fields": hints.priority_fields,
            "kb_context": hints.kb_context,
            "refresh_interval_days": hints.refresh_interval_days,
            "min_confidence": hints.min_confidence,
            "max_variations_discovery": hints.max_variations_discovery,
            "max_variations_targeted": hints.max_variations_targeted,
        }

    def list_domains(self) -> list[str]:
        if not self._root.exists():
            return []
        domains = []
        for p in self._root.iterdir():
            if p.is_dir() and (p / "spec.yaml").exists():
                domains.append(p.name)
            elif p.suffix in (".yaml", ".yml") and p.stem != "__pycache__":
                domains.append(p.stem)
        return domains

    def _parse(self, domain_id: str, raw: dict) -> DomainEnrichmentConfig:
        identity = raw.get("domain_identity", raw.get("domain", {}))
        version = identity.get("version", raw.get("version", "0.1.0-seed"))

        ontology = raw.get("ontology", {})
        nodes_raw = ontology.get("nodes", [])
        node_schemas: dict[str, NodeSchema] = {}

        for node_def in nodes_raw:
            label = node_def.get("label", "")
            props_raw = node_def.get("properties", [])
            props: dict[str, str] = {}
            required: list[str] = []

            for p in props_raw:
                if isinstance(p, dict):
                    name = p.get("name", "")
                    ptype = p.get("type", "string")
                    props[name] = ptype
                    if p.get("required", False):
                        required.append(name)
                elif isinstance(p, str):
                    props[p] = "string"

            hints_raw = node_def.get("enrichment_hints", {})
            hints = None
            if hints_raw:
                hints = EnrichmentHints(
                    objective_template=hints_raw.get("objective_template", ""),
                    priority_fields=hints_raw.get("priority_fields", []),
                    kb_context=hints_raw.get("kb_context"),
                    refresh_interval_days=hints_raw.get("refresh_interval_days", 90),
                    min_confidence=hints_raw.get("min_confidence", 0.75),
                    max_variations_discovery=hints_raw.get("max_variations_discovery", 5),
                    max_variations_targeted=hints_raw.get("max_variations_targeted", 3),
                )

            node_schemas[label] = NodeSchema(
                label=label,
                properties=props,
                required_fields=required,
                enrichment_hints=hints,
            )

        return DomainEnrichmentConfig(
            domain_id=domain_id,
            version=version,
            node_schemas=node_schemas,
            inference_rules_path=raw.get("inference_rules_path"),
            raw_spec=raw,
        )
