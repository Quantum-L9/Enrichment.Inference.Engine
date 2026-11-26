"""
Schema Discovery — Proposes new fields + schema version evolution.

After convergence, analyzes what was discovered vs. what the original
schema defined, and produces PropertySpec proposals for schema evolution.

Aligned with L9 Contract Specifications domain spec versioning:
  0.1.0-seed → 0.2.0-discovered → 0.3.0-inferred → 0.4.0-proposed

Output is a SchemaProposal returned to the caller only: the `discover` action
(`handlers.handle_discover`) and `POST /api/v1/discover` put it in their
response. No code emits it as a `schema_proposal` TransportPacket, writes it
back to Odoo, or feeds it to CEG's domain loader; a proposal reaches a tenant
spec only through the reviewed promotion path (Quantum-L9/Cognitive.Engine.Graphs#400).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PropertySpec:
    """A proposed field addition to the domain schema."""

    name: str
    field_type: str  # "string" | "float" | "integer" | "boolean" | "enum" | "list"
    discovered_by: str  # "enrichment" | "inference"
    discovery_confidence: float
    managed_by: str = "enrichment"  # or "computed" for inferred
    derived_from: list[str] = field(default_factory=list)
    sample_values: list[Any] = field(default_factory=list)
    fill_rate: float = 0.0  # across batch: % of entities that have this field
    auto_proposed: bool = True


@dataclass
class SchemaProposal:
    """Proposed schema evolution from a convergence run."""

    current_version: str
    proposed_version: str
    stage: str  # "discovered" | "inferred" | "proposed"
    new_properties: list[PropertySpec] = field(default_factory=list)
    proposed_gates: list[dict] = field(default_factory=list)
    proposed_scoring: list[dict] = field(default_factory=list)
    entity_count: int = 0
    fill_rate_threshold: float = 0.75


class SchemaDiscoveryEngine:
    """
    Analyzes convergence results to propose schema evolution.
    """

    GATE_THRESHOLD = 0.75

    def __init__(self, current_schema: dict[str, str] | None = None, version: str = "0.1.0-seed"):
        self._current = current_schema or {}
        self._version = version

    def analyze(
        self,
        enriched_fields: dict[str, Any],
        inferred_fields: dict[str, Any],
        confidence_map: dict[str, float],
        batch_stats: dict[str, float] | None = None,
    ) -> SchemaProposal:
        """
        Compare enriched+inferred fields against current schema.
        Propose new fields that don't exist in the current schema.
        """
        fill_rates = batch_stats or {}
        new_props = self._build_enriched_props(enriched_fields, confidence_map, fill_rates)
        new_props += self._build_inferred_props(inferred_fields, confidence_map, fill_rates)
        # INV-8 / ADR-010: CEG owns match gates and scoring; EIE proposes fields
        # only, as convergence/schema_proposer.py already does. The keys stay on
        # the proposal (always empty) so the discover response shape is unchanged.
        proposed_gates: list[dict] = []

        stage = (
            "inferred" if any(p.discovered_by == "inference" for p in new_props) else "discovered"
        )
        proposed_version = self._bump_version(stage)

        return SchemaProposal(
            current_version=self._version,
            proposed_version=proposed_version,
            stage=stage,
            new_properties=new_props,
            proposed_gates=proposed_gates,
            entity_count=1,
            fill_rate_threshold=self.GATE_THRESHOLD,
        )

    def _build_enriched_props(
        self,
        enriched_fields: dict[str, Any],
        confidence_map: dict[str, float],
        fill_rates: dict[str, float],
    ) -> list[PropertySpec]:
        """Build PropertySpec list for enrichment-discovered fields not in current schema."""
        props: list[PropertySpec] = []
        for fname, value in enriched_fields.items():
            if fname not in self._current:
                props.append(
                    PropertySpec(
                        name=fname,
                        field_type=self._infer_type(value),
                        discovered_by="enrichment",
                        discovery_confidence=confidence_map.get(fname, 0.5),
                        managed_by="enrichment",
                        sample_values=[value] if value is not None else [],
                        fill_rate=fill_rates.get(fname, 0.0),
                    )
                )
        return props

    def _build_inferred_props(
        self,
        inferred_fields: dict[str, Any],
        confidence_map: dict[str, float],
        fill_rates: dict[str, float],
    ) -> list[PropertySpec]:
        """Build PropertySpec list for inference-derived fields not in current schema."""
        props: list[PropertySpec] = []
        for fname, value in inferred_fields.items():
            if fname not in self._current:
                props.append(
                    PropertySpec(
                        name=fname,
                        field_type=self._infer_type(value),
                        discovered_by="inference",
                        discovery_confidence=confidence_map.get(fname, 0.7),
                        managed_by="computed",
                        derived_from=self._find_dependencies(fname),
                        sample_values=[value] if value is not None else [],
                        fill_rate=fill_rates.get(fname, 0.0),
                    )
                )
        return props

    def _infer_type(self, value: Any) -> str:
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "float"
        if isinstance(value, list):
            return "list"
        return "string"

    def _find_dependencies(self, _field_name: str = "") -> list[str]:
        """Placeholder — in production, traced from InferenceBridge rule_trace."""
        return []

    def _bump_version(self, stage: str) -> str:
        parts = self._version.split("-")[0].split(".")
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
        return f"{major}.{minor + 1}.{patch}-{stage}"
