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

from app.utils.safe_convert import safe_float

logger = structlog.get_logger(__name__)

_KERNEL_NAME_INVARIANT_EVENT = "schema_proposer.kernel_name_invariant_violation"
_KERNEL_SECTION = "L9 §3.1/§5.1/§5.2"

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
_NODE_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class FieldProposal(BaseModel):
    field_name: str
    node_label: str | None = None
    discovery_spec_sha256: str | None = None
    ownership_contract_version: int | None = None
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
    tenant_id: str | None = None
    discovery_spec_sha256: str | None = None
    ownership_contract_version: int | None = None
    proposed_fields: list[FieldProposal] = Field(default_factory=list)
    proposed_gates: list[GateProposal] = Field(default_factory=list)
    proposed_scoring_dimensions: list[ScoringDimensionProposal] = Field(default_factory=list)
    yaml_diff: str = ""
    version_bump: str = ""
    entities_analysed: int = 0


class ApprovalDecision(BaseModel):
    field_name: str
    node_label: str | None = None
    approved: bool = True
    reason: str = ""


def propose(
    batch_results: list[dict[str, Any]],
    current_yaml: dict[str, Any],
    domain: str = "",
    *,
    node_label: str | None = None,
    discovery_spec_sha256: str | None = None,
    tenant_id: str | None = None,
) -> SchemaProposalSet:
    """Discover evidence under explicit owners; unbound calls are draft diagnostics.

    The caller obtains the digest from exact discovery bytes before accumulation.
    No provider source, object type, or sole-node heuristic supplies an owner.
    """
    bound = (
        node_label is not None
        or discovery_spec_sha256 is not None
        or tenant_id is not None
        or any(result.get("node_label") is not None for result in batch_results)
    )
    resolved_domain = domain or current_yaml.get("name", current_yaml.get("domain", ""))
    if not isinstance(resolved_domain, str):
        resolved_domain = ""
    proposed_fields: list[FieldProposal] = []
    if bound:
        _validate_tenant_domain(tenant_id, resolved_domain, current_yaml)
        _validate_discovery_digest(discovery_spec_sha256)
        nodes = current_yaml.get("ontology", {}).get("nodes", {})
        if node_label is not None:
            _validate_node_label(node_label)
            _target_node_def(nodes, node_label)
        results_by_owner: dict[str, list[dict[str, Any]]] = {}
        # Validate the entire batch before collecting any evidence.
        for result in batch_results:
            result_owner = result.get("node_label")
            if node_label is not None and result_owner not in (None, node_label):
                raise ValueError("Result node_label conflicts with discovery node_label")
            owner = _validate_node_label(node_label if node_label is not None else result_owner)
            _target_node_def(nodes, owner)
            results_by_owner.setdefault(owner, []).append(result)
        ambiguous: dict[str, set[str]] = {}
        for owner, results in results_by_owner.items():
            existing_fields = _extract_existing_fields(current_yaml, node_label=owner)
            field_stats: dict[str, _FieldAcc] = {}
            for result in results:
                _process_result_fields(result, existing_fields, field_stats)
            try:
                owner_fields = _build_field_proposals(field_stats, len(results))
            except SourceAttributionError as exc:
                ambiguous.update(
                    {f"{owner}.{name}": set(sources) for name, sources in exc.field_sources}
                )
                continue
            for fp in owner_fields:
                fp.node_label = owner
                fp.discovery_spec_sha256 = discovery_spec_sha256
                fp.ownership_contract_version = 1
            proposed_fields.extend(owner_fields)
        if ambiguous:
            raise SourceAttributionError(ambiguous)
    else:
        if not batch_results:
            return SchemaProposalSet(domain=domain)
        existing_fields = _extract_existing_fields(current_yaml)
        field_stats = {}
        for result in batch_results:
            _process_result_fields(result, existing_fields, field_stats)
        proposed_fields = _build_field_proposals(field_stats, len(batch_results))
    proposed_fields.sort(key=lambda p: (-p.fill_rate, -p.avg_confidence))

    # ADR-010: EIE discovers evidence fields, never CEG ranking policy.
    proposed_gates: list[GateProposal] = []
    proposed_dims: list[ScoringDimensionProposal] = []

    current_version = current_yaml.get("version", "0.1.0")
    version_bump = _bump_version(current_version)
    yaml_diff = _generate_yaml_diff(proposed_fields, proposed_gates, proposed_dims)

    return SchemaProposalSet(
        domain=resolved_domain,
        tenant_id=tenant_id if bound else None,
        discovery_spec_sha256=discovery_spec_sha256 if bound else None,
        ownership_contract_version=1 if bound else None,
        proposed_fields=proposed_fields,
        proposed_gates=proposed_gates,
        proposed_scoring_dimensions=proposed_dims,
        yaml_diff=yaml_diff,
        version_bump=version_bump,
        entities_analysed=len(batch_results),
    )


def apply(
    current_yaml: dict[str, Any],
    approval_decisions: list[ApprovalDecision],
    proposal_set: SchemaProposalSet,
    *,
    node_label: str | None = None,
    allow_overwrite: bool = False,
    source_digest: str | None = None,
    source_sha256: str | None = None,
) -> dict[str, Any]:
    """Apply recorded owners atomically, preserving metadata and ADR-010.

    Native execution requires bound fields/decisions and an exact current-source
    digest assertion, even on a rejected/no-op batch. ``node_label`` is only an
    equality assertion over every proposal owner, never a destination selector.
    Legacy non-native draft helpers remain diagnostic compatibility seams.
    """
    bound = _has_ownership_binding(proposal_set)
    if "name" in current_yaml or bound:
        validate_ownership(proposal_set, approval_decisions)
        _validate_tenant_domain(proposal_set.tenant_id, proposal_set.domain, current_yaml)
        digest = source_sha256 if source_sha256 is not None else source_digest
        if (
            source_sha256 is not None
            and source_digest is not None
            and source_sha256 != source_digest
        ):
            raise ValueError("Conflicting discovery source digest assertions")
        if _validate_discovery_digest(digest) != proposal_set.discovery_spec_sha256:
            raise ValueError("Stale discovery spec digest: current source differs")
        nodes = current_yaml.get("ontology", {}).get("nodes", {})
        if node_label is not None:
            _validate_node_label(node_label)
            _target_node_def(nodes, node_label)
        for fp in proposal_set.proposed_fields:
            _target_node_def(nodes, fp.node_label)
            if node_label is not None and fp.node_label != node_label:
                raise ValueError("node_label assertion conflicts with recorded proposal owner")
        applied_keys = applied_field_keys(approval_decisions, proposal_set)
        applied_count = len(applied_keys)
    else:
        applied_names = applied_field_names(approval_decisions, proposal_set)
        applied_count = len(applied_names)
        applied_keys = []
    if not applied_count:
        logger.info(
            "schema_proposer.apply: no approved proposal applies (proposed=%d); version stays %s",
            len(proposal_set.proposed_fields),
            current_yaml.get("version"),
        )
        return copy.deepcopy(current_yaml)
    updated = copy.deepcopy(current_yaml)

    ontology = updated.setdefault("ontology", {})
    nodes = ontology.setdefault("nodes", {})
    if bound:
        selected_keys = set(applied_keys)
        for fp in proposal_set.proposed_fields:
            if (fp.node_label, fp.field_name) in selected_keys:
                _apply_node_properties(
                    nodes,
                    {fp.field_name},
                    [fp],
                    node_label=fp.node_label,
                    allow_overwrite=allow_overwrite,
                )
    else:
        _apply_node_properties(
            nodes,
            set(applied_names),
            proposal_set.proposed_fields,
            node_label=node_label,
            allow_overwrite=allow_overwrite,
        )

    updated["version"] = proposal_set.version_bump

    logger.info(
        "schema_proposer.apply: approved=%d/%d version=%s",
        applied_count,
        len(proposal_set.proposed_fields),
        proposal_set.version_bump,
    )
    return updated


def _validate_node_label(value: object) -> str:
    if not isinstance(value, str) or not _NODE_LABEL_RE.fullmatch(value):
        raise ValueError(
            "Explicit bound node_label must be a valid identifier of at most 128 characters"
        )
    return value


def _validate_discovery_digest(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ValueError("Discovery spec digest must be a lowercase SHA-256 digest")
    return value


def _validate_tenant_domain(
    tenant: object, domain: str, current_yaml: dict[str, Any] | None = None
) -> None:
    if not isinstance(tenant, str) or not tenant or domain != tenant:
        raise ValueError("Bound proposal tenant_id must equal domain")
    if current_yaml is not None and current_yaml.get("name", tenant) != tenant:
        raise ValueError("Bound proposal tenant/domain does not match native spec")


def _has_ownership_binding(proposal_set: SchemaProposalSet) -> bool:
    return (
        proposal_set.tenant_id is not None
        or proposal_set.discovery_spec_sha256 is not None
        or proposal_set.ownership_contract_version is not None
        or any(
            fp.node_label is not None
            or fp.discovery_spec_sha256 is not None
            or fp.ownership_contract_version is not None
            for fp in proposal_set.proposed_fields
        )
    )


def validate_ownership(
    proposal_set: SchemaProposalSet,
    approval_decisions: list[ApprovalDecision],
) -> dict[tuple[str, str], bool]:
    """Preflight binding and pair membership without writing or inferring owners."""
    if proposal_set.ownership_contract_version != 1:
        raise ValueError(
            "Native proposals must be explicitly owner-bound with node_label (contract version 1)"
        )
    _validate_tenant_domain(proposal_set.tenant_id, proposal_set.domain)
    digest = _validate_discovery_digest(proposal_set.discovery_spec_sha256)
    keys: set[tuple[str, str]] = set()
    for fp in proposal_set.proposed_fields:
        key = (_validate_node_label(fp.node_label), fp.field_name)
        if fp.ownership_contract_version != 1 or fp.discovery_spec_sha256 != digest:
            raise ValueError(
                "Every native field must retain the proposal's owner-bound discovery digest"
            )
        if key in keys:
            raise ValueError("Duplicate proposed owner/field pair")
        keys.add(key)
    decisions: dict[tuple[str, str], bool] = {}
    for decision in approval_decisions:
        key = (_validate_node_label(decision.node_label), decision.field_name)
        if key in decisions:
            raise ValueError("Duplicate approval owner/field pair")
        if key not in keys:
            raise ValueError("Approval decision names an unproposed owner/field pair")
        if not _is_canonical_field_name(decision.field_name):
            raise ValueError("Approval decision names a noncanonical field")
        decisions[key] = decision.approved
    return decisions


def applied_field_keys(
    approval_decisions: list[ApprovalDecision],
    proposal_set: SchemaProposalSet,
) -> list[tuple[str, str]]:
    """Return applying owner/field identities in proposal order; refuse unbound input."""
    decisions = validate_ownership(proposal_set, approval_decisions)
    return [
        (_validate_node_label(fp.node_label), fp.field_name)
        for fp in proposal_set.proposed_fields
        if decisions.get((_validate_node_label(fp.node_label), fp.field_name), False)
        and _is_canonical_field_name(fp.field_name)
    ]


def applied_field_names(
    approval_decisions: list[ApprovalDecision],
    proposal_set: SchemaProposalSet,
) -> list[str]:
    """The proposed fields :func:`apply` writes, in proposal order.

    A field applies when a decision approves it, ``proposal_set`` proposes it,
    and its name is canonical (L9 §3.1 / §5.1 / §5.2). An approval answers this
    count, not the count of approving decisions.
    """
    if _has_ownership_binding(proposal_set):
        keys = applied_field_keys(approval_decisions, proposal_set)
        if len({fp.node_label for fp in proposal_set.proposed_fields}) > 1:
            raise ValueError("Multi-owner proposals require applied_field_keys, not field names")
        return [name for _, name in keys]
    approved = {d.field_name for d in approval_decisions if d.approved}
    return [
        fp.field_name
        for fp in proposal_set.proposed_fields
        if fp.field_name in approved and _is_canonical_field_name(fp.field_name)
    ]


def _is_canonical_field_name(name: str) -> bool:
    """Per L9 Master Kernel §3.1 + §5.1 + §5.2: snake_case form, not in drift list."""
    if name in BANNED_FIELD_NAMES_L9_KERNEL:
        return False
    return bool(_SNAKE_CASE_RE.fullmatch(name))


def _confidence_records(confidences_raw: Any) -> dict[str, Any] | None:
    """The per-field records of a ``FieldConfidenceMap``-like object, or None.

    ``FieldConfidenceMap`` (app/models/field_confidence.py) keeps them under
    ``fields``; the earlier look-up read ``entries``, which no model has, so the
    live approve path -- which passes ``LoopState.accumulated_confidences`` --
    saw every confidence as 0.0 and proposed nothing (EIE-IDENT-03).
    """
    for attr in ("fields", "entries"):
        records = getattr(confidences_raw, attr, None)
        if isinstance(records, dict):
            return records
    return None


def _build_confidence_map(confidences_raw: Any) -> dict[str, float]:
    """Extract a {field_name: confidence} map from various confidences_raw formats."""
    records = _confidence_records(confidences_raw)
    if records is not None:
        return {
            name: safe_float(getattr(rec, "confidence", 0.0))
            for name, rec in records.items()
            if isinstance(name, str)
        }
    if isinstance(confidences_raw, dict):
        entries = confidences_raw.get("entries", confidences_raw)
        conf_map: dict[str, float] = {}
        for k, v in entries.items():
            if isinstance(v, dict):
                conf_map[k] = safe_float(v.get("confidence", 0.0))
            else:
                conf_map[k] = safe_float(v) if isinstance(v, (int, float)) else 0.0
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
    observations: dict[str, list[str]] | None = None
    if "field_source_observations" in result:
        raw_observations = result["field_source_observations"]
        if not isinstance(raw_observations, dict):
            raise ValueError("Field source observations must be a field-keyed mapping")
        observations = {}
        for name, sources in raw_observations.items():
            if (
                not isinstance(name, str)
                or not isinstance(sources, list)
                or any(
                    not isinstance(source, str) or not source.strip() or source != source.strip()
                    for source in sources
                )
            ):
                raise ValueError("Field source observations must be lists of nonblank strings")
            observations[name] = sources

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
            if observations is not None:
                sources = observations.get(field_name, [])
                # Live observed provenance is complete. Missing observations are
                # unsupported evidence, not permission for an enrichment default.
                acc.confidences.append(conf_map.get(field_name, 0.0) if sources else 0.0)
                acc.sources.update(sources)
                continue
            acc.confidences.append(conf_map.get(field_name, 0.0))
            records = _confidence_records(confidences_raw)
            if records is not None:
                record = records.get(field_name)
                source = getattr(record, "source", None)
                if source:
                    acc.sources.add(str(getattr(source, "value", source)))
            elif isinstance(confidences_raw, dict):
                entry = confidences_raw.get("entries", confidences_raw).get(field_name, {})
                if isinstance(entry, dict):
                    acc.sources.add(entry.get("source", "enrichment"))


class SourceAttributionError(ValueError):
    """Refuse a proposal call while retaining every qualifying source conflict."""

    def __init__(self, sources_by_field: dict[str, set[str]]) -> None:
        self.field_sources: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
            (name, tuple(sorted(sources))) for name, sources in sorted(sources_by_field.items())
        )
        evidence = "; ".join(f"{name}={list(sources)!r}" for name, sources in self.field_sources)
        super().__init__(f"Ambiguous source attribution: {evidence}")


def _build_field_proposals(
    field_stats: dict[str, _FieldAcc],
    entity_count: int,
) -> list[FieldProposal]:
    """Filter, refuse ambiguous attribution, then build evidence field proposals."""
    qualifying: list[tuple[str, _FieldAcc, float, float]] = []
    ambiguous: dict[str, set[str]] = {}
    for field_name, acc in field_stats.items():
        fill_rate = acc.non_null / entity_count if entity_count > 0 else 0.0
        avg_conf = statistics.mean(acc.confidences) if acc.confidences else 0.0
        if fill_rate < MIN_FILL_RATE or avg_conf < MIN_AVG_CONFIDENCE:
            continue
        if not _is_canonical_field_name(field_name):
            logger.warning(
                _KERNEL_NAME_INVARIANT_EVENT,
                field_name=field_name,
                entity_count=acc.non_null,
                stage="propose",
                kernel_section=_KERNEL_SECTION,
            )
            continue
        qualifying.append((field_name, acc, fill_rate, avg_conf))
        if len(acc.sources) > 1:
            ambiguous[field_name] = acc.sources
    # Preflight the whole qualifying set before constructing or returning any
    # proposal. Sorting is evidence presentation, never a source-priority rule.
    if ambiguous:
        raise SourceAttributionError(ambiguous)

    proposals: list[FieldProposal] = []
    for field_name, acc, fill_rate, avg_conf in qualifying:
        field_type = _infer_type(acc.values)
        distribution = _compute_distribution(acc.values, field_type)
        if acc.sources:
            (source,) = acc.sources
        else:
            source = "enrichment"
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


def _target_node_def(nodes: Any, node_label: str | None) -> dict[str, Any]:
    """Select one node without silently assigning a field to the wrong entity."""
    if isinstance(nodes, dict):
        candidates = [
            value
            for label, value in nodes.items()
            if isinstance(value, dict) and (node_label is None or label == node_label)
        ]
    elif isinstance(nodes, list):
        candidates = [
            value
            for value in nodes
            if isinstance(value, dict) and (node_label is None or value.get("label") == node_label)
        ]
    else:
        candidates = []
    if len(candidates) != 1:
        raise ValueError("node_label must identify exactly one ontology node")
    return candidates[0]


def _set_property(
    props: Any,
    field_name: str,
    definition: dict[str, Any],
    *,
    allow_overwrite: bool = False,
) -> None:
    """Add a field, or explicitly update its type/source without losing metadata."""
    if isinstance(props, dict):
        if field_name in props:
            if not allow_overwrite:
                raise ValueError(f"Property {field_name!r} already exists")
            existing = props[field_name]
            if isinstance(existing, dict):
                _retype(existing, definition)
                return
        props[field_name] = definition
        return
    if isinstance(props, list):
        for entry in props:
            if isinstance(entry, dict) and entry.get("name") == field_name:
                if not allow_overwrite:
                    raise ValueError(f"Property {field_name!r} already exists")
                _retype(entry, definition)
                return
        props.append({"name": field_name, **definition})


def _retype(existing: dict[str, Any], definition: dict[str, Any]) -> None:
    """Update a declaration in place; a new type never inherits the old type's nesting."""
    for nested in ("items", "properties"):
        if nested not in definition:
            existing.pop(nested, None)
    existing.update(definition)


def _typed_definition(fp: FieldProposal) -> dict[str, Any]:
    """The declaration a proposal adds, in the shared typed syntax (spec_types).

    A ``list`` always declares its ``items`` (an untyped list is refused by
    the native reader): inferred from the proposal's sample values, else
    ``string`` -- what untyped list values were always coerced to.
    """
    definition: dict[str, Any] = {"type": fp.field_type, "source": fp.source}
    if fp.field_type == "list":
        definition["items"] = _infer_items(fp.sample_values)
    return definition


def _infer_scalar(values: list[Any]) -> str | None:
    if values and all(isinstance(v, bool) for v in values):
        return "bool"
    if values and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        return "float"
    if values and all(isinstance(v, str) for v in values):
        return "string"
    return None


def _infer_items(samples: list[Any]) -> dict[str, Any]:
    """Item declaration for a list field from its sample lists (see _typed_definition)."""
    members = [m for sample in samples if isinstance(sample, list) for m in sample if m is not None]
    if members and all(_is_named_map(m) for m in members):
        return _infer_map_items(members)
    return {"type": _infer_scalar(members) or "string"}


def _is_named_map(member: Any) -> bool:
    return isinstance(member, dict) and bool(member) and all(isinstance(k, str) for k in member)


def _infer_map_items(members: list[dict[str, Any]]) -> dict[str, Any]:
    """A map item declaration, or string when a key or value is not declarable."""
    properties = []
    for key in sorted({k for m in members for k in m}):
        scalar = _infer_scalar([m[key] for m in members if m.get(key) is not None])
        if scalar is None or not re.fullmatch(r"[A-Za-z_]\w*", key, re.ASCII):
            return {"type": "string"}
        properties.append({"name": key, "type": scalar})
    return {"type": "map", "properties": properties}


def _apply_node_properties(
    nodes: Any,
    approved_fields: set[str],
    proposed_fields: list[FieldProposal],
    *,
    node_label: str | None = None,
    allow_overwrite: bool = False,
) -> None:
    """Merge approved field proposals into the explicitly selected node.

    EIE-IDENT-03: the live specs declare ``ontology.nodes`` as a list of node
    definitions whose ``properties`` is a list of ``{name, type}`` entries; the
    earlier dict-only walk silently applied nothing to them, so an approved
    revision never reached the spec. Both shapes are served, each written back
    in its own shape.
    """
    node_def = _target_node_def(nodes, node_label)
    props = node_def.get("properties")
    if not isinstance(props, (dict, list)):
        props = node_def["properties"] = [] if isinstance(nodes, list) else {}
    for fp in proposed_fields:
        if fp.field_name in approved_fields:
            if not _is_canonical_field_name(fp.field_name):
                logger.warning(
                    _KERNEL_NAME_INVARIANT_EVENT,
                    field_name=fp.field_name,
                    stage="apply",
                    kernel_section=_KERNEL_SECTION,
                )
                continue
            _set_property(
                props,
                fp.field_name,
                _typed_definition(fp),
                allow_overwrite=allow_overwrite,
            )


def _apply_gate_proposals(
    gates: list,
    approved_fields: set[str],
    proposed_gates: list[GateProposal],
) -> None:
    """Legacy compatibility seam: CEG gate application is prohibited (ADR-010)."""
    return None


def _apply_scoring_proposals(
    scoring: list,
    approved_fields: set[str],
    proposed_dims: list[ScoringDimensionProposal],
) -> None:
    """Legacy compatibility seam: CEG scoring application is prohibited (ADR-010)."""
    return None


class _FieldAcc:
    __slots__ = ("total", "non_null", "values", "confidences", "sources")

    def __init__(self) -> None:
        self.total: int = 0
        self.non_null: int = 0
        self.values: list[Any] = []
        self.confidences: list[float] = []
        self.sources: set[str] = set()


def _extract_existing_fields(
    yaml_spec: dict[str, Any], *, node_label: str | None = None
) -> set[str]:
    """Existing properties of the recorded owner; unbound drafts use the old union."""
    fields: set[str] = set()
    ontology = yaml_spec.get("ontology", {})
    if not isinstance(ontology, dict):
        return fields
    nodes = ontology.get("nodes", ontology.get("entities", {}))
    node_defs: list[Any] = (
        [_target_node_def(nodes, node_label)]
        if node_label is not None
        else (
            list(nodes.values())
            if isinstance(nodes, dict)
            else nodes
            if isinstance(nodes, list)
            else []
        )
    )
    for node_def in node_defs:
        if not isinstance(node_def, dict):
            continue
        props = node_def.get("properties", {})
        if isinstance(props, dict):
            fields.update(props.keys())
        elif isinstance(props, list):
            for entry in props:
                if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    fields.add(entry["name"])
                elif isinstance(entry, str):
                    fields.add(entry)
    return fields


def revision_from_approved(
    current_yaml: dict[str, Any],
    *,
    fields: list[FieldProposal],
    gates: list[GateProposal],
    dims: list[ScoringDimensionProposal],
    domain: str = "",
    tenant_id: str | None = None,
    discovery_spec_sha256: str | None = None,
    ownership_contract_version: int | None = None,
) -> SchemaProposalSet:
    """A proposal set that re-applies proposals already approved and persisted.

    EIE-IDENT-03 read side: ``apply`` takes a :class:`SchemaProposalSet`, so a
    persisted revision (``schema_proposals`` rows with ``approval_status =
    approved``) is rebuilt into one against the spec it is being replayed onto.
    The version bump is computed from ``current_yaml`` the same way ``propose``
    computes it, so a replayed approval lands on the same version it did live.
    """
    declared = current_yaml.get("name", current_yaml.get("domain", ""))
    return SchemaProposalSet(
        domain=domain or (declared if isinstance(declared, str) else ""),
        tenant_id=tenant_id,
        discovery_spec_sha256=discovery_spec_sha256,
        ownership_contract_version=ownership_contract_version,
        proposed_fields=fields,
        proposed_gates=[],
        proposed_scoring_dimensions=[],
        yaml_diff=_generate_yaml_diff(fields, [], []),
        version_bump=_bump_version(str(current_yaml.get("version", "0.1.0"))),
        entities_analysed=0,
    )


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
        nums = [safe_float(v) for v in values if isinstance(v, (int, float))]
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
    """Legacy compatibility seam: EIE never proposes CEG gates (ADR-010)."""
    return []


def _propose_scoring_dimensions(fields: list[FieldProposal]) -> list[ScoringDimensionProposal]:
    """Legacy compatibility seam: EIE never proposes CEG scoring (ADR-010)."""
    return []


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
    """Describe evidence fields only, even for historical ranking payloads."""
    lines: list[str] = ["# Proposed Schema Additions", ""]
    if fields:
        lines.append("## New Properties")
        for fp in fields:
            identity = (
                f"{fp.node_label}.{fp.field_name}" if fp.node_label is not None else fp.field_name
            )
            lines.append(f"  {identity}:")
            lines.append(f"    type: {fp.field_type}")
            lines.append(f"    source: {fp.source}")
            lines.append(
                f"    # fill_rate: {fp.fill_rate:.0%}, confidence: {fp.avg_confidence:.2f}"
            )
            lines.append("")
    return "\n".join(lines)
