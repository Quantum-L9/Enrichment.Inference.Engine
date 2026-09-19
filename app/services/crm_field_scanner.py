"""
CRM Field Scanner — Intake Module for the Enrichment-Inference Convergence Loop.

File 9 of 12 | app/services/crm_field_scanner.py

Scans a customer's existing CRM field schema against the domain YAML ontology.
Produces:
  1. ScanResult — matched / unmapped / missing field classification
  2. Seed YAML — v0.1.0-seed domain YAML containing only customer's current fields
  3. DiscoveryReport — the sales document that converts Seed → Enrich ($500/mo)

Dependencies:
  - domain YAML reader (exists)
  - field_classifier.py (exists)
  - schemas.py (exists)

L9 Contract Compliance:
  - No FastAPI imports (handler registration via chassis)
  - No eval/exec
  - No stubs or TODOs
  - structlog for structured logging (C-04)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# ── Enums ────────────────────────────────────────────────────


class FieldMatchStatus(StrEnum):
    MATCHED = "matched"
    UNMAPPED = "unmapped"
    MISSING = "missing"


class ImpactTier(StrEnum):
    GATE_CRITICAL = "gate_critical"
    SCORING_CRITICAL = "scoring_critical"
    INFERENCE_INPUT = "inference_input"
    ENRICHABLE = "enrichable"
    NICE_TO_HAVE = "nice_to_have"


# ── Data Models ──────────────────────────────────────────────


@dataclass
class CRMField:
    """One source CRM field.

    ``name`` is the source system's technical field name. ``source_system`` /
    ``source_resource`` record which system and which resource (e.g. Odoo
    ``res.partner`` vs ``crm.lead``) supplied the field, so identically named
    fields from different resources stay distinguishable. ``fill_rate`` is a
    *sample* fill rate over the bounded records the source adapter inspected,
    never a database-global statistic; ``None`` means no observations.
    """

    name: str
    field_type: str = "string"
    sample_values: list[Any] = field(default_factory=list)
    fill_rate: float | None = None
    source_system: str | None = None
    source_resource: str | None = None


@dataclass
class DomainProperty:
    name: str
    field_type: str = "string"
    managed_by: str | None = None
    is_gate: bool = False
    is_scoring: bool = False
    is_inference_input: bool = False
    is_inference_output: bool = False
    derived_from: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class FieldMapping:
    crm_field: str | None
    domain_property: str | None
    status: FieldMatchStatus
    type_compatible: bool = True
    impact_tier: ImpactTier = ImpactTier.NICE_TO_HAVE
    source_system: str | None = None
    source_resource: str | None = None


@dataclass
class ScanResult:
    domain_id: str
    domain_version: str
    total_crm_fields: int
    total_domain_properties: int
    matched: list[FieldMapping] = field(default_factory=list)
    unmapped: list[FieldMapping] = field(default_factory=list)
    missing: list[FieldMapping] = field(default_factory=list)
    coverage_ratio: float = 0.0
    scan_hash: str = ""

    @property
    def matched_count(self) -> int:
        return len(self.matched)

    @property
    def unmapped_count(self) -> int:
        return len(self.unmapped)

    @property
    def missing_count(self) -> int:
        return len(self.missing)

    @property
    def gate_critical_missing(self) -> list[FieldMapping]:
        return [m for m in self.missing if m.impact_tier == ImpactTier.GATE_CRITICAL]

    @property
    def scoring_critical_missing(self) -> list[FieldMapping]:
        return [m for m in self.missing if m.impact_tier == ImpactTier.SCORING_CRITICAL]


@dataclass
class DiscoveryReportEntry:
    field_name: str
    field_type: str
    impact_tier: ImpactTier
    impact_rank: int
    description: str
    why_it_matters: str
    acquisition_method: str
    estimated_fill_rate: float | None = None


@dataclass
class DiscoveryReport:
    domain_id: str
    customer_fields: int
    domain_fields: int
    coverage_pct: float
    missing_entries: list[DiscoveryReportEntry] = field(default_factory=list)
    gate_blocked_count: int = 0
    scoring_degraded_count: int = 0
    inference_blocked_count: int = 0
    estimated_enrichment_cost_usd: float = 0.0
    report_hash: str = ""


# ── Domain YAML Parsing ─────────────────────────────────────


def _collect_gate_fields(domain_spec: dict[str, Any]) -> set[str]:
    """Extract all property names referenced in gate definitions."""
    gate_fields: set[str] = set()
    for gate in domain_spec.get("gates", []):
        for key in ("candidate_property", "candidate_prop", "query_param"):
            val = gate.get(key)
            if val and isinstance(val, str):
                gate_fields.add(val)
    return gate_fields


def _collect_scoring_fields(domain_spec: dict[str, Any]) -> set[str]:
    """Extract all property names referenced in scoring dimension definitions."""
    scoring_fields: set[str] = set()
    dims = domain_spec.get(
        "scoring_dimensions", domain_spec.get("scoring", {}).get("dimensions", [])
    )
    for dim in dims if isinstance(dims, list) else []:
        if isinstance(dim, dict):
            for key in ("candidate_property", "candidate_prop", "source"):
                val = dim.get(key)
                if val and isinstance(val, str):
                    scoring_fields.add(val)
    return scoring_fields


def _collect_inference_fields(domain_spec: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Extract inference input and output field names from inference rules."""
    inputs: set[str] = set()
    outputs: set[str] = set()
    for rule in domain_spec.get("inference_rules", []):
        for cond in rule.get("conditions", []):
            f = cond.get("field")
            if f:
                inputs.add(f)
        for out in rule.get("outputs", []):
            f = out.get("field")
            if f:
                outputs.add(f)
    return inputs, outputs


def _iter_node_defs(domain_spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Iterate over raw node/entity definition dicts in the domain ontology."""
    ontology = domain_spec.get("ontology", domain_spec)
    nodes = ontology.get("nodes", ontology.get("entities", []))
    if isinstance(nodes, dict):
        return [v for v in nodes.values() if isinstance(v, dict)]
    if isinstance(nodes, list):
        return [n for n in nodes if isinstance(n, dict)]
    return []


def _props_from_node(
    node_def: dict[str, Any],
    gate_fields: set[str],
    scoring_fields: set[str],
    inference_inputs: set[str],
    inference_outputs: set[str],
) -> list[DomainProperty]:
    """Parse all DomainProperty entries from a single ontology node definition."""
    props = node_def.get("properties", {})
    result: list[DomainProperty] = []
    if isinstance(props, dict):
        for name, prop_def in props.items():
            result.append(
                _parse_domain_property(
                    name, prop_def, gate_fields, scoring_fields, inference_inputs, inference_outputs
                )
            )
    elif isinstance(props, list):
        for prop_def in props:
            if isinstance(prop_def, dict):
                name = prop_def.get("name", "")
                if name:
                    result.append(
                        _parse_domain_property(
                            name,
                            prop_def,
                            gate_fields,
                            scoring_fields,
                            inference_inputs,
                            inference_outputs,
                        )
                    )
    return result


def _extract_domain_properties(
    domain_spec: dict[str, Any],
) -> list[DomainProperty]:
    gate_fields = _collect_gate_fields(domain_spec)
    scoring_fields = _collect_scoring_fields(domain_spec)
    inference_inputs, inference_outputs = _collect_inference_fields(domain_spec)

    properties: list[DomainProperty] = []
    for node_def in _iter_node_defs(domain_spec):
        properties.extend(
            _props_from_node(
                node_def, gate_fields, scoring_fields, inference_inputs, inference_outputs
            )
        )
    return properties


def _parse_domain_property(
    name: str,
    prop_def: Any,
    gate_fields: set[str],
    scoring_fields: set[str],
    inference_inputs: set[str],
    inference_outputs: set[str],
) -> DomainProperty:
    if not isinstance(prop_def, dict):
        return DomainProperty(
            name=name,
            field_type=str(prop_def) if prop_def else "string",
            is_gate=name in gate_fields,
            is_scoring=name in scoring_fields,
            is_inference_input=name in inference_inputs,
            is_inference_output=name in inference_outputs,
        )
    return DomainProperty(
        name=name,
        field_type=prop_def.get("type", "string"),
        managed_by=prop_def.get("managed_by"),
        is_gate=name in gate_fields,
        is_scoring=name in scoring_fields,
        is_inference_input=name in inference_inputs,
        is_inference_output=name in inference_outputs,
        derived_from=prop_def.get("derived_from", []) or [],
        description=prop_def.get("description", ""),
    )


# ── Field Name Normalization ────────────────────────────────


def _normalize(name: str) -> str:
    norm = name.strip().lower()
    for prefix in ("x_", "custom_", "cf_", "c_", "__c"):
        if norm.startswith(prefix):
            norm = norm[len(prefix) :]
            break
    return norm.replace("-", "_").replace(" ", "_").strip("_")


def _type_compatible(crm_type: str, domain_type: str) -> bool:
    crm_norm = crm_type.lower().strip()
    dom_norm = domain_type.lower().strip()
    if crm_norm == dom_norm:
        return True
    numeric = {"float", "integer", "int", "number", "decimal", "double", "currency"}
    text = {"string", "text", "char", "varchar", "html", "selection"}
    boolean = {"bool", "boolean"}
    collection = {"list", "array", "many2many", "one2many", "tags"}
    for group in (numeric, text, boolean, collection):
        if crm_norm in group and dom_norm in group:
            return True
    return {crm_norm, dom_norm} <= {"enum", "string", "selection"}


# ── Impact Classification ───────────────────────────────────


def _classify_impact(prop: DomainProperty) -> ImpactTier:
    if prop.is_gate:
        return ImpactTier.GATE_CRITICAL
    if prop.is_scoring:
        return ImpactTier.SCORING_CRITICAL
    if prop.is_inference_input:
        return ImpactTier.INFERENCE_INPUT
    if prop.managed_by in ("computed", "derived", "inference"):
        return ImpactTier.NICE_TO_HAVE
    return ImpactTier.ENRICHABLE


_IMPACT_RANK: dict[ImpactTier, int] = {
    ImpactTier.GATE_CRITICAL: 1,
    ImpactTier.SCORING_CRITICAL: 2,
    ImpactTier.INFERENCE_INPUT: 3,
    ImpactTier.ENRICHABLE: 4,
    ImpactTier.NICE_TO_HAVE: 5,
}


# ── Core Scanner ────────────────────────────────────────────


def scan_crm_fields(
    crm_fields: list[CRMField],
    domain_spec: dict[str, Any],
) -> ScanResult:
    """Map CRM fields against domain ontology. Classify matched/unmapped/missing."""
    domain_meta = domain_spec.get("domain", domain_spec.get("metadata", {}))
    domain_id = (
        (domain_meta.get("id", "") or domain_meta.get("name", "unknown"))
        if isinstance(domain_meta, dict)
        else "unknown"
    )
    domain_version = (
        domain_meta.get("version", "0.0.0") if isinstance(domain_meta, dict) else "0.0.0"
    )

    domain_properties = _extract_domain_properties(domain_spec)
    domain_lookup: dict[str, DomainProperty] = {_normalize(p.name): p for p in domain_properties}

    matched: list[FieldMapping] = []
    unmapped: list[FieldMapping] = []
    matched_domain_keys: set[str] = set()

    for crm_field in crm_fields:
        crm_norm = _normalize(crm_field.name)
        if crm_norm in domain_lookup:
            prop = domain_lookup[crm_norm]
            matched_domain_keys.add(crm_norm)
            matched.append(
                FieldMapping(
                    crm_field=crm_field.name,
                    domain_property=prop.name,
                    status=FieldMatchStatus.MATCHED,
                    type_compatible=_type_compatible(crm_field.field_type, prop.field_type),
                    impact_tier=_classify_impact(prop),
                    source_system=crm_field.source_system,
                    source_resource=crm_field.source_resource,
                )
            )
        else:
            unmapped.append(
                FieldMapping(
                    crm_field=crm_field.name,
                    domain_property=None,
                    status=FieldMatchStatus.UNMAPPED,
                    source_system=crm_field.source_system,
                    source_resource=crm_field.source_resource,
                )
            )

    missing: list[FieldMapping] = []
    for norm_name, prop in domain_lookup.items():
        if norm_name not in matched_domain_keys:
            missing.append(
                FieldMapping(
                    crm_field=None,
                    domain_property=prop.name,
                    status=FieldMatchStatus.MISSING,
                    impact_tier=_classify_impact(prop),
                )
            )
    missing.sort(key=lambda m: _IMPACT_RANK.get(m.impact_tier, 99))

    total_domain = len(domain_properties)
    # Coverage counts unique domain properties covered, not mappings: two source
    # resources both supplying `phone` keep both provenance-bearing mappings but
    # cover the domain property once (ratio stays within [0, 1]).
    coverage = len(matched_domain_keys) / total_domain if total_domain > 0 else 0.0
    # Scan identity includes source provenance so identically named fields from
    # different resources (res.partner.phone vs crm.lead.phone) hash independently.
    # Legacy fields without provenance contribute empty markers and stay deterministic.
    field_identities = sorted(
        (f.source_system or "", f.source_resource or "", f.name, f.field_type) for f in crm_fields
    )
    scan_input = f"{domain_id}:{domain_version}:{field_identities}"
    scan_hash = hashlib.sha256(scan_input.encode()).hexdigest()[:16]

    result = ScanResult(
        domain_id=domain_id,
        domain_version=domain_version,
        total_crm_fields=len(crm_fields),
        total_domain_properties=total_domain,
        matched=matched,
        unmapped=unmapped,
        missing=missing,
        coverage_ratio=round(coverage, 4),
        scan_hash=scan_hash,
    )
    logger.info(
        "crm_scan_complete",
        extra={
            "domain": domain_id,
            "matched": result.matched_count,
            "missing": result.missing_count,
            "coverage": result.coverage_ratio,
        },
    )
    return result


# ── Seed YAML Generator ─────────────────────────────────────


def generate_seed_yaml(
    scan_result: ScanResult,
    domain_template: dict[str, Any],
) -> dict[str, Any]:
    """Produce v0.1.0-seed domain YAML with only customer's matched CRM fields."""
    template_props = _extract_domain_properties(domain_template)
    template_lookup = {_normalize(tp.name): tp for tp in template_props}

    seed_properties: list[dict[str, Any]] = []
    for mapping in scan_result.matched:
        if not mapping.domain_property:
            continue
        prop_entry: dict[str, Any] = {"name": mapping.domain_property, "type": "string"}
        tp = template_lookup.get(_normalize(mapping.domain_property))
        if tp:
            prop_entry["type"] = tp.field_type
            if tp.managed_by:
                prop_entry["managed_by"] = tp.managed_by
            if tp.description:
                prop_entry["description"] = tp.description
        seed_properties.append(prop_entry)

    domain_meta = domain_template.get("domain", domain_template.get("metadata", {}))
    base_id = (
        domain_meta.get("id", scan_result.domain_id)
        if isinstance(domain_meta, dict)
        else scan_result.domain_id
    )

    seed_yaml: dict[str, Any] = {
        "domain": {"id": f"customer-{base_id}", "name": base_id, "version": "0.1.0-seed"},
        "ontology": {"nodes": [{"label": "Partner", "properties": seed_properties}]},
    }
    logger.info(
        "seed_yaml_generated",
        extra={"domain": seed_yaml["domain"]["id"], "properties": len(seed_properties)},
    )
    return seed_yaml


# ── Discovery Report Generator ──────────────────────────────


_ESTIMATED_TOKENS_PER_FIELD = 500
_TOKEN_RATE_PER_1K = 0.005


def _acquisition_method(prop: DomainProperty) -> str:
    if prop.managed_by in ("computed", "derived", "inference") or prop.is_inference_output:
        return "inference"
    return "enrichment"


def _why_it_matters(prop: DomainProperty, impact: ImpactTier) -> str:
    reasons = {
        ImpactTier.GATE_CRITICAL: f"'{prop.name}' is a gate filter — missing blocks all matching.",
        ImpactTier.SCORING_CRITICAL: f"'{prop.name}' is a scoring dimension — missing degrades rank quality.",
        ImpactTier.INFERENCE_INPUT: f"'{prop.name}' feeds inference rules — missing blocks grade/tier derivation.",
        ImpactTier.ENRICHABLE: f"'{prop.name}' can be discovered via AI enrichment.",
        ImpactTier.NICE_TO_HAVE: f"'{prop.name}' provides additional entity context.",
    }
    return reasons.get(impact, f"'{prop.name}' is a domain property.")


def generate_discovery_report(
    scan_result: ScanResult,
    domain_spec: dict[str, Any],
    entity_count: int = 1,
) -> DiscoveryReport:
    """Generate the sales-facing discovery report for Seed → Enrich conversion."""
    domain_properties = _extract_domain_properties(domain_spec)
    prop_lookup = {_normalize(p.name): p for p in domain_properties}

    entries: list[DiscoveryReportEntry] = []
    gate_blocked = scoring_degraded = inference_blocked = enrichable_count = 0

    for rank, mapping in enumerate(scan_result.missing, start=1):
        if not mapping.domain_property:
            continue
        prop = prop_lookup.get(
            _normalize(mapping.domain_property), DomainProperty(name=mapping.domain_property)
        )
        impact = mapping.impact_tier
        method = _acquisition_method(prop)

        if impact == ImpactTier.GATE_CRITICAL:
            gate_blocked += 1
        elif impact == ImpactTier.SCORING_CRITICAL:
            scoring_degraded += 1
        elif impact == ImpactTier.INFERENCE_INPUT:
            inference_blocked += 1
        if method == "enrichment":
            enrichable_count += 1

        entries.append(
            DiscoveryReportEntry(
                field_name=mapping.domain_property,
                field_type=prop.field_type,
                impact_tier=impact,
                impact_rank=rank,
                description=prop.description or f"Domain property: {prop.name}",
                why_it_matters=_why_it_matters(prop, impact),
                acquisition_method=method,
            )
        )

    estimated_cost = (
        enrichable_count * entity_count * _ESTIMATED_TOKENS_PER_FIELD * _TOKEN_RATE_PER_1K / 1000
    )
    report_hash = hashlib.sha256(
        f"{scan_result.scan_hash}:{len(entries)}:{entity_count}".encode()
    ).hexdigest()[:16]

    return DiscoveryReport(
        domain_id=scan_result.domain_id,
        customer_fields=scan_result.total_crm_fields,
        domain_fields=scan_result.total_domain_properties,
        coverage_pct=round(scan_result.coverage_ratio * 100, 1),
        missing_entries=entries,
        gate_blocked_count=gate_blocked,
        scoring_degraded_count=scoring_degraded,
        inference_blocked_count=inference_blocked,
        estimated_enrichment_cost_usd=round(estimated_cost, 2),
        report_hash=report_hash,
    )


# ── Serialization for TransportPacket ────────────────────────


def scan_result_to_dict(result: ScanResult) -> dict[str, Any]:
    return {
        "domain_id": result.domain_id,
        "domain_version": result.domain_version,
        "total_crm_fields": result.total_crm_fields,
        "total_domain_properties": result.total_domain_properties,
        "matched_count": result.matched_count,
        "unmapped_count": result.unmapped_count,
        "missing_count": result.missing_count,
        "coverage_ratio": result.coverage_ratio,
        "scan_hash": result.scan_hash,
        "matched": [
            {
                "crm_field": m.crm_field,
                "domain_property": m.domain_property,
                "type_compatible": m.type_compatible,
                "source_system": m.source_system,
                "source_resource": m.source_resource,
            }
            for m in result.matched
        ],
        "unmapped": [
            {
                "crm_field": m.crm_field,
                "source_system": m.source_system,
                "source_resource": m.source_resource,
            }
            for m in result.unmapped
        ],
        "missing": [
            {"domain_property": m.domain_property, "impact_tier": m.impact_tier.value}
            for m in result.missing
        ],
    }


def discovery_report_to_dict(report: DiscoveryReport) -> dict[str, Any]:
    return {
        "domain_id": report.domain_id,
        "customer_fields": report.customer_fields,
        "domain_fields": report.domain_fields,
        "coverage_pct": report.coverage_pct,
        "gate_blocked_count": report.gate_blocked_count,
        "scoring_degraded_count": report.scoring_degraded_count,
        "estimated_enrichment_cost_usd": report.estimated_enrichment_cost_usd,
        "report_hash": report.report_hash,
        "missing_fields": [
            {
                "field_name": e.field_name,
                "field_type": e.field_type,
                "impact_tier": e.impact_tier.value,
                "impact_rank": e.impact_rank,
                "why_it_matters": e.why_it_matters,
                "acquisition_method": e.acquisition_method,
            }
            for e in report.missing_entries
        ],
        "summary": {
            "headline": f"You have {report.customer_fields} fields. Your domain needs {report.domain_fields}. You're missing {len(report.missing_entries)} fields.",
            "matching_impact": f"{report.gate_blocked_count} gate-critical fields missing — entities cannot be matched."
            if report.gate_blocked_count > 0
            else "No gate-critical fields missing.",
            "enrichment_estimate": f"Estimated cost: ${report.estimated_enrichment_cost_usd:,.2f}",
        },
    }
