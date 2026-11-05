"""
Domain YAML Reader — loads and parses domain specification YAML files.

Provides DomainSpec dataclass for domain configuration including:
- Field definitions and types
- Enrichment rules and constraints
- KB fragment mappings
- Inference rule references
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.engines.native_yaml import parse_native_yaml


@dataclass
class FieldSpec:
    """Field specification from domain YAML."""

    name: str
    type: str
    required: bool = False
    description: str = ""
    enrichment_priority: int = 0
    kb_fragments: list[str] = field(default_factory=list)


@dataclass
class DomainSpec:
    """
    Domain specification loaded from YAML.

    Contains field definitions, enrichment rules, KB mappings,
    and inference rule references for a specific domain.
    """

    domain_id: str
    version: str = "1.0.0"
    description: str = ""
    fields: list[FieldSpec] = field(default_factory=list)
    kb_fragments: dict[str, str] = field(default_factory=dict)
    inference_rules: list[str] = field(default_factory=list)
    enrichment_config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: Path) -> DomainSpec:
        """Load DomainSpec from YAML file."""
        data = parse_native_yaml(path.read_bytes())

        fields = [
            FieldSpec(
                name=fd["name"],
                type=fd.get("type", "string"),
                required=fd.get("required", False),
                description=fd.get("description", ""),
                enrichment_priority=fd.get("enrichment_priority", 0),
                kb_fragments=fd.get("kb_fragments", []),
            )
            for fd in data.get("fields", [])
        ]

        return cls(
            domain_id=data.get("domain_id", "unknown"),
            version=data.get("version", "1.0.0"),
            description=data.get("description", ""),
            fields=fields,
            kb_fragments=data.get("kb_fragments", {}),
            inference_rules=data.get("inference_rules", []),
            enrichment_config=data.get("enrichment_config", {}),
        )

    def get_field(self, name: str) -> FieldSpec | None:
        """Get field spec by name."""
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def get_required_fields(self) -> list[str]:
        """Get list of required field names."""
        return [f.name for f in self.fields if f.required]

    def get_enrichment_priority_fields(self, top_n: int = 10) -> list[str]:
        """Get top N fields by enrichment priority."""
        sorted_fields = sorted(self.fields, key=lambda f: f.enrichment_priority, reverse=True)
        return [f.name for f in sorted_fields[:top_n]]
