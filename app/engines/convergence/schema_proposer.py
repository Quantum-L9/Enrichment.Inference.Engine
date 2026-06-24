"""Schema proposer — aggregates convergence discoveries into YAML evolution proposals.

This is the Discover tier ($2K/mo) unlock. After running the convergence loop
on a batch, the system proposes schema changes based on what it found.
"""

from __future__ import annotations

import copy
import re
import statistics
from typing import Any

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)

MIN_FILL_RATE = 0.60
MIN_AVG_CONFIDENCE = 0.70
MAX_SAMPLE_VALUES = 10

# L9 Master Kernel §3.1 + §5.1 + §5.2 — banned non-canonical field name forms.
# Single source of truth derived from the kernel. NOT per-domain or per-tenant.
BANNED_FIELD_NAMES_L9_KERNEL: frozenset[str] = frozenset(
    {
        # §5.1 — TransportPacket / shared contract drift forms
        "packetid",
        "packetID",
        "packettype",
        "packetType",
        "contentHash",
        "content_hash_sha256",
        "threadId",
        "threadID",
        "traceId",
        "traceID",
        "parentIds",
        "sourceNode",
        "onBehalfOf",
        "orgId",
        "orgID",
        # §5.2 — spec.yaml drift forms (incl. valid-snake_case-but-wrong synonyms)
        "matchentities",
        "nodelabels",
        "matchdirections",
        "candidateprop",
        "null_semantics",
        "computation_type",
        "targetnode",
        "idproperty",
    }
)

# L9 Master Kernel §3.1 — canonical snake_case form for any proposed field name.
_SNAKE_CASE_RE: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9_]*$")


class FieldProposal(BaseModel):
    field_name: str
    field_type: str = "string"
    source: str = "enrichment"
    fill_rate: float = 0.0
    avg_confidence: float = 0.0
    sample_values: list[Any] = Field(default_factory=list)
    value_distribution: dict[str, Any] = Field(default_factory=dict)
    entity_count: int = 0


class GateProposal(BaseModel):
    field_name: str
    gate_type: str = "categorical"
    rationale: str = ""
    distinct_values: int = 0


class ScoringDimensionProposal(BaseModel):
    field_name: str
    dimension_type: str = "numeric_distance"
    min_value: float = 0.0
    max_value: float = 0.0
    mean_value: float = 0.0
    stddev: float = 0.0
    rationale: str = ""


class SchemaProposalSet(BaseModel):
    domain: str = ""
    proposed_fields: list[FieldProposal] = Field(default_factory=list)
    proposed_gates: list[GateProposal] = Field(default_factory=list)
    proposed_scoring_dimensions: list[ScoringDimensionProposal] = Field(default_factory=list)
    yaml_diff: str = ""
    version_bump: str = ""
    entities_analysed: int = 0


class ApprovalDecision(BaseModel):
    field_name: str
    approved: bool = True
    reason: str = ""


def propose(
    batch_results: list[dict[str, Any]],
    current_yaml: dict[str, Any],
    domain: str = "",
) -> SchemaProposalSet:
    """Aggregate discovered fields across a batch and propose schema changes."""
    if not batch_results:
        return SchemaProposalSet(domain=domain)

    existing_fields = _extract_existing_fields(current_yaml)
    field_stats: dict[str, _FieldAcc] = {}

    for result in batch_results:
        _process_result_fields(result, existing_fields, field_stats)

    entity_count = len(batch_results)
    proposed_fields = _build_field_proposals(field_stats, entity_count)
    proposed_fields.sort(key=lambda p: (-p.fill_rate, -p.avg_confidence))

    proposed_gates = _propose_gates(proposed_fields)
    proposed_dims = _propose_scoring_dimensions(proposed_fields)

    current_version = current_yaml.get("version", "0.1.0")
    version_bump = _bump_version(current_version)
    yaml_diff = _generate_yaml_diff(proposed_fields, proposed_gates, proposed_dims)

    return SchemaProposalSet(
        domain=domain or current_yaml.get("domain", ""),
        proposed_fields=proposed_fields,
        proposed_gates=proposed_gates,
        proposed_scoring_dimensions=proposed_dims,
        yaml_diff=yaml_diff,
        version_bump=version_bump,
        entities_analysed=entity_count,
    )


def apply(
    current_yaml: dict[str, Any],
    approval_decisions: list[ApprovalDecision],
    proposal_set: SchemaProposalSet,
) -> dict[str, Any]:
    """Apply approved proposals to the domain YAML, returning the updated spec."""
    approved_fields = {d.field_name for d in approval_decisions if d.approved}
    updated = copy.deepcopy(current_yaml)

    ontology = updated.setdefault("ontology", {})
    nodes = ontology.setdefault("nodes", {})
    _apply_node_properties(nodes, approved_fields, proposal_set.proposed_fields)

    gates = updated.setdefault("gates", [])
    if isinstance(gates, list):
        _apply_gate_proposals(gates, approved_fields, proposal_set.proposed_gates)

    scoring = updated.setdefault("scoring", [])
    if isinstance(scoring, list):
        _apply_scoring_proposals(scoring, approved_fields, proposal_set.proposed_scoring_dimensions)

    updated["version"] = proposal_set.version_bump

    logger.info(
        "schema_proposer.apply: approved=%d/%d version=%s",
        len(approved_fields),
        len(proposal_set.proposed_fields),
        proposal_set.version_bump,
    )
    return updated


def _is_canonical_field_name(name: str) -> bool:
    """Per L9 Master Kernel §3.1 + §5.1 + §5.2: snake_case form, not in drift list."""
    if name in BANNED_FIELD_NAMES_L9_KERNEL:
        return False
    return bool(_SNAKE_CASE_RE.fullmatch(name))


def _build_confidence_map(confidences_raw: Any) -> dict[str, float]:
    """Extract a {field_name: confidence} map from various confidences_raw formats."""
    if hasattr(confidences_raw, "entries"):
        return {e.field_name: e.confidence for e in confidences_raw.entries.values()}
    if isinstance(confidences_raw, dict):
        entries = confidences_raw.get("entries", confidences_raw)
        conf_map: dict[str, float] = {}
        for k, v in entries.items():
            if isinstance(v, dict):
                conf_map[k] = float(v.get("confidence", 0.0))
            else:
                conf_map[k] = float(v) if isinstance(v, (int, float)) else 0.0
        return conf_map
    return {}


def _process_result_fields(
    result: dict[str, Any],
    existing_fields: set[str],
    field_stats: dict[str, _FieldAcc],
) -> None:
    """Accumulate field stats from a single batch result into field_stats."""
    final_fields = result.get("final_fields", {})
    confidences_raw = result.get("final_field_confidences", {})
    conf_map = _build_confidence_map(confidences_raw)

    for field_name, value in final_fields.items():
        if field_name in existing_fields:
            continue
        if field_name not in field_stats:
            field_stats[field_name] = _FieldAcc()
        acc = field_stats[field_name]
        acc.total += 1
        if value is not None:
            acc.non_null += 1
            acc.values.append(value)
            acc.confidences.append(conf_map.get(field_name, 0.0))
            if isinstance(confidences_raw, dict):
                entry = confidences_raw.get("entries", confidences_raw).get(field_name, {})
                if isinstance(entry, dict):
                    acc.sources.add(entry.get("source", "enrichment"))


def _build_field_proposals(
    field_stats: dict[str, _FieldAcc],
    entity_count: int,
) -> list[FieldProposal]:
    """Filter and build FieldProposal objects from accumulated field stats."""
    proposals: list[FieldProposal] = []
    for field_name, acc in field_stats.items():
        fill_rate = acc.non_null / entity_count if entity_count > 0 else 0.0
        avg_conf = statistics.mean(acc.confidences) if acc.confidences else 0.0
        if fill_rate < MIN_FILL_RATE or avg_conf < MIN_AVG_CONFIDENCE:
            continue
        if not _is_canonical_field_name(field_name):
            logger.warning(
                "schema_proposer.kernel_name_invariant_violation",
                field_name=field_name,
                entity_count=acc.non_null,
                stage="propose",
                kernel_section="L9 §3.1/§5.1/§5.2",
            )
            continue
        field_type = _infer_type(acc.values)
        distribution = _compute_distribution(acc.values, field_type)
        source = next(iter(acc.sources)) if acc.sources else "enrichment"
        proposals.append(
            FieldProposal(
                field_name=field_name,
                field_type=field_type,
                source=source,
                fill_rate=round(fill_rate, 4),
                avg_confidence=round(avg_conf, 4),
                sample_values=acc.values[:MAX_SAMPLE_VALUES],
                value_distribution=distribution,
                entity_count=acc.non_null,
            )
        )
    return proposals


def _apply_node_properties(
    nodes: dict,
    approved_fields: set[str],
    proposed_fields: list[FieldProposal],
) -> None:
    """Merge approved field proposals into the first ontology node's properties."""
    first_node_key = next(iter(nodes), None) if isinstance(nodes, dict) else None
    if first_node_key and isinstance(nodes, dict):
        node_def = nodes[first_node_key]
        if isinstance(node_def, dict):
            props = node_def.setdefault("properties", {})
            for fp in proposed_fields:
                if fp.field_name in approved_fields:
                    if not _is_canonical_field_name(fp.field_name):
                        logger.warning(
                            "schema_proposer.kernel_name_invariant_violation",
                            field_name=fp.field_name,
                            stage="apply",
                            kernel_section="L9 §3.1/§5.1/§5.2",
                        )
                        continue
                    props[fp.field_name] = {"type": fp.field_type, "source": fp.source}


def _apply_gate_proposals(
    gates: list,
    approved_fields: set[str],
    proposed_gates: list[GateProposal],
) -> None:
    """Append approved gate proposals to the gates list."""
    for gp in proposed_gates:
        if gp.field_name in approved_fields:
            if not _is_canonical_field_name(gp.field_name):
                logger.warning(
                    "schema_proposer.kernel_name_invariant_violation",
                    field_name=gp.field_name,
                    stage="apply",
                    kernel_section="L9 §3.1/§5.1/§5.2",
                )
                continue
            gates.append({"field": gp.field_name, "type": gp.gate_type, "auto_proposed": True})


def _apply_scoring_proposals(
    scoring: list,
    approved_fields: set[str],
    proposed_dims: list[ScoringDimensionProposal],
) -> None:
    """Append approved scoring dimension proposals to the scoring list."""
    for sp in proposed_dims:
        if sp.field_name in approved_fields:
            if not _is_canonical_field_name(sp.field_name):
                logger.warning(
                    "schema_proposer.kernel_name_invariant_violation",
                    field_name=sp.field_name,
                    stage="apply",
                    kernel_section="L9 §3.1/§5.1/§5.2",
                )
                continue
            scoring.append(
                {"field": sp.field_name, "type": sp.dimension_type, "auto_proposed": True}
            )


class _FieldAcc:
    __slots__ = ("total", "non_null", "values", "confidences", "sources")

    def __init__(self) -> None:
        self.total: int = 0
        self.non_null: int = 0
        self.values: list[Any] = []
        self.confidences: list[float] = []
        self.sources: set[str] = set()


def _extract_existing_fields(yaml_spec: dict[str, Any]) -> set[str]:
    fields: set[str] = set()
    ontology = yaml_spec.get("ontology", {})
    nodes = ontology.get("nodes", ontology.get("entities", {}))
    if isinstance(nodes, dict):
        for _node_type, node_def in nodes.items():
            if isinstance(node_def, dict):
                fields.update(node_def.get("properties", {}).keys())
    return fields


def _infer_type(values: list[Any]) -> str:
    if not values:
        return "string"
    float_count = sum(1 for v in values if isinstance(v, (int, float)) and not isinstance(v, bool))
    bool_count = sum(1 for v in values if isinstance(v, bool))
    list_count = sum(1 for v in values if isinstance(v, (list, tuple)))

    if float_count > len(values) * 0.7:
        return "float"
    if bool_count > len(values) * 0.7:
        return "boolean"
    if list_count > len(values) * 0.5:
        return "list"
    return "string"


def _compute_distribution(values: list[Any], field_type: str) -> dict[str, Any]:
    if not values:
        return {}
    if field_type == "float":
        nums = [float(v) for v in values if isinstance(v, (int, float))]
        if not nums:
            return {}
        return {
            "min": round(min(nums), 4),
            "max": round(max(nums), 4),
            "mean": round(statistics.mean(nums), 4),
            "stddev": round(statistics.stdev(nums), 4) if len(nums) > 1 else 0.0,
        }
    if field_type == "string":
        from collections import Counter

        str_vals = [str(v) for v in values]
        top = Counter(str_vals).most_common(5)
        return {"top_values": [{"value": v, "count": c} for v, c in top]}
    if field_type == "boolean":
        true_count = sum(1 for v in values if v is True)
        return {"true_pct": round(true_count / len(values), 4)}
    return {}


def _propose_gates(fields: list[FieldProposal]) -> list[GateProposal]:
    gates: list[GateProposal] = []
    for fp in fields:
        if fp.field_type == "string" and fp.value_distribution:
            top = fp.value_distribution.get("top_values", [])
            distinct = len(top)
            if 2 <= distinct <= 20:
                gates.append(
                    GateProposal(
                        field_name=fp.field_name,
                        gate_type="categorical",
                        rationale=f"Categorical with {distinct} distinct values, {fp.fill_rate:.0%} fill rate",
                        distinct_values=distinct,
                    )
                )
        elif fp.field_type == "boolean":
            gates.append(
                GateProposal(
                    field_name=fp.field_name,
                    gate_type="boolean",
                    rationale=f"Boolean gate with {fp.fill_rate:.0%} fill rate",
                    distinct_values=2,
                )
            )
    return gates


def _propose_scoring_dimensions(fields: list[FieldProposal]) -> list[ScoringDimensionProposal]:
    dims: list[ScoringDimensionProposal] = []
    for fp in fields:
        if fp.field_type == "float" and fp.value_distribution:
            dist = fp.value_distribution
            stddev = dist.get("stddev", 0.0)
            if stddev > 0:
                dims.append(
                    ScoringDimensionProposal(
                        field_name=fp.field_name,
                        dimension_type="numeric_distance",
                        min_value=dist.get("min", 0.0),
                        max_value=dist.get("max", 0.0),
                        mean_value=dist.get("mean", 0.0),
                        stddev=stddev,
                        rationale=f"Numeric with meaningful variance (σ={stddev:.4f}), {fp.fill_rate:.0%} fill",
                    )
                )
    return dims


def _bump_version(current: str) -> str:
    base = current.split("-")[0]
    parts = base.split(".")
    try:
        major, minor, _patch = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        return "0.2.0-discovered"
    return f"{major}.{minor + 1}.0-discovered"


def _generate_yaml_diff(
    fields: list[FieldProposal],
    gates: list[GateProposal],
    dims: list[ScoringDimensionProposal],
) -> str:
    lines: list[str] = ["# Proposed Schema Additions", ""]
    if fields:
        lines.append("## New Properties")
        for fp in fields:
            lines.append(f"  {fp.field_name}:")
            lines.append(f"    type: {fp.field_type}")
            lines.append(f"    source: {fp.source}")
            lines.append(
                f"    # fill_rate: {fp.fill_rate:.0%}, confidence: {fp.avg_confidence:.2f}"
            )
            lines.append("")
    if gates:
        lines.append("## Proposed Gates")
        for gp in gates:
            lines.append(f"  - field: {gp.field_name}")
            lines.append(f"    type: {gp.gate_type}")
            lines.append(f"    # {gp.rationale}")
            lines.append("")
    if dims:
        lines.append("## Proposed Scoring Dimensions")
        for sp in dims:
            lines.append(f"  - field: {sp.field_name}")
            lines.append(f"    type: {sp.dimension_type}")
            lines.append(f"    # {sp.rationale}")
            lines.append("")
    return "\n".join(lines)
