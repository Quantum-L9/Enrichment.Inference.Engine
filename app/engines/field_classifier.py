"""
Field Difficulty Classifier v2 (field_classifier.py)

Fully domain-agnostic. Zero hardcoded industry data.
All domain knowledge comes from the YAML spec — nothing else.

Consumed YAML contract (see bottom of file for full schema):
  domain_spec.yaml must contain:
    - ontology.nodes: either the native cartridge form (a list of nodes, each
      with `properties` as a list of `{name, type, ...}` declarations) or the
      legacy form (`properties` as a `{field_name: {...}}` mapping)
    - search_sources (optional): {difficulty: [domains]}
    - gate_fields (optional): [field_names]
    - scoring_fields (optional): [field_names]
    - time_sensitive_fields (optional): [field_names]
    - ambiguous_fields (optional): [field_names]

Integration:
  domain_spec.yaml → auto_classify_domain() → DomainClassification
  DomainClassification → search_optimizer_v2.resolve() → SonarConfig
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog

from .domain_yaml_reader import declared_spec_identity

logger = structlog.get_logger("field_classifier")


class FieldDifficulty(StrEnum):
    TRIVIAL = "trivial"
    PUBLIC = "public"
    FINDABLE = "findable"
    OBSCURE = "obscure"
    INFERRABLE = "inferrable"


# ──────────────────────────────────────────────
# Rule-based classifier — zero LLM cost
# ──────────────────────────────────────────────

# Metadata signals that mark a field as inferrable (domain-agnostic)
INFERRABLE_SIGNALS: set[str] = {
    "managed_by:computed",
    "managed_by:derived",
    "managed_by:inference",
    "source:inference",
    "source:computed",
    "source:rule_engine",
}

# Universal name-pattern banks (cross-industry heuristics)
# These are structural patterns, not domain-specific vocabulary
INFERRABLE_NAME_PATTERNS: set[str] = {
    "grade",
    "tier",
    "class",
    "score",
    "rating",
    "rank",
    "classification",
    "category",
    "level",
    "bucket",
    "segment",
    "index",
    "percentile",
    "quartile",
}
TRIVIAL_NAME_PATTERNS: set[str] = {
    "name",
    "legal_name",
    "dba",
    "address",
    "street",
    "city",
    "state",
    "zip",
    "postal",
    "country",
    "phone",
    "fax",
    "email",
    "contact_name",
    "contact_email",
    "contact_phone",
    "website",
    "url",
    "domain",
}
PUBLIC_NAME_PATTERNS: set[str] = {
    "revenue",
    "employee",
    "headcount",
    "staff_count",
    "founded",
    "year_founded",
    "incorporation",
    "naics",
    "sic",
    "ein",
    "tax_id",
    "duns",
    "ownership",
    "parent_company",
    "subsidiary",
    "stock_ticker",
    "market_cap",
    "geographic_reach",
    "headquarters",
}
OBSCURE_NAME_PATTERNS: set[str] = {
    "capacity",
    "throughput",
    "tonnage",
    "volume_annual",
    "sqft",
    "square_feet",
    "facility_size",
    "lot_size",
    "equipment",
    "machinery",
    "line_count",
    "permit",
    "license_number",
    "waste_gen_id",
    "energy_consumption",
    "water_usage",
    "emission",
}


@dataclass
class FieldMeta:
    """Extracted metadata about a single field from the domain YAML."""

    name: str
    field_type: str = "string"
    managed_by: str | None = None
    source: str | None = None
    derived_from: list[str] | None = None
    discovery_confidence: float | None = None
    difficulty_override: str | None = None  # explicit override from YAML
    is_gate: bool = False
    is_scoring: bool = False
    is_time_sensitive: bool = False
    description: str = ""
    examples: list[str] | None = None


# ──────────────────────────────────────────────
# YAML extraction
# ──────────────────────────────────────────────


def _read_set(spec: dict[str, Any], key: str) -> set[str]:
    """Safely read a list from the YAML and return as set."""
    val = spec.get(key, [])
    if isinstance(val, (list, set)):
        return set(val)
    return set()


def spec_domain_name(domain_spec: dict[str, Any]) -> str:
    """The identity the loaded spec declares, never a guess.

    A native cartridge declares top-level `name`; legacy specs use `domain` or
    `metadata.domain`. A spec that declares none is `unknown`.
    """
    declared = declared_spec_identity(domain_spec)[0]
    if declared:
        return declared
    metadata = domain_spec.get("metadata")
    legacy = metadata.get("domain") if isinstance(metadata, dict) else None
    return legacy if isinstance(legacy, str) and legacy else "unknown"


def _iter_props(props: Any) -> Iterator[tuple[str, Any]]:
    """(name, declaration) pairs from either supported `properties` form.

    Native: a list of `{name, type, ...}` declarations (a bare string is a
    string field). Legacy: a `{name: declaration}` mapping. An entry that names
    no field is skipped; nothing is invented for it.
    """
    if isinstance(props, dict):
        yield from props.items()
    elif isinstance(props, list):
        for entry in props:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"]:
                yield entry["name"], entry
            elif isinstance(entry, str) and entry:
                yield entry, "string"


def _extract_props_from_nodes(
    node_defs: Any,
    gates: set[str],
    scoring: set[str],
    time_sensitive: set[str],
) -> list[FieldMeta]:
    """FieldMeta for every property of every node, in declaration order."""
    fields: list[FieldMeta] = []
    for node_def in node_defs:
        if not isinstance(node_def, dict):
            continue
        for prop_name, prop_def in _iter_props(node_def.get("properties", {})):
            fields.append(_parse_prop(prop_name, prop_def, gates, scoring, time_sensitive))
    return fields


def extract_field_meta(domain_spec: dict[str, Any]) -> list[FieldMeta]:
    """Pull every property from the domain YAML ontology with its metadata.

    Reads gate_fields, scoring_fields, time_sensitive_fields from the
    top-level YAML keys — no external arguments needed.
    """
    gates = _read_set(domain_spec, "gate_fields")
    scoring = _read_set(domain_spec, "scoring_fields")
    time_sensitive = _read_set(domain_spec, "time_sensitive_fields")

    ontology = domain_spec.get("ontology", domain_spec)
    nodes = ontology.get("nodes", ontology.get("entities", {}))

    if isinstance(nodes, dict):
        return _extract_props_from_nodes(nodes.values(), gates, scoring, time_sensitive)
    if isinstance(nodes, list):
        return _extract_props_from_nodes(nodes, gates, scoring, time_sensitive)
    return []


def _parse_prop(
    name: str,
    prop_def: Any,
    gates: set[str],
    scoring: set[str],
    time_sensitive: set[str],
) -> FieldMeta:
    if isinstance(prop_def, dict):
        return FieldMeta(
            name=name,
            field_type=prop_def.get("type", "string"),
            managed_by=prop_def.get("managed_by"),
            source=prop_def.get("source"),
            derived_from=prop_def.get("derived_from"),
            discovery_confidence=prop_def.get("discovery_confidence"),
            difficulty_override=prop_def.get("difficulty"),  # NEW: explicit
            is_gate=name in gates,
            is_scoring=name in scoring,
            is_time_sensitive=name in time_sensitive,
            description=prop_def.get("description", ""),
            examples=prop_def.get("examples"),
        )
    return FieldMeta(name=name, field_type=str(prop_def))


# ──────────────────────────────────────────────
# Core classifier — deterministic, zero cost
# ──────────────────────────────────────────────


def _normalise(name: str) -> str:
    return name.strip().lower().replace("-", "_").replace(" ", "_")


def _classify_by_metadata_signals(meta: FieldMeta) -> FieldDifficulty | None:
    """Return INFERRABLE if metadata signals indicate derivation, else None."""
    if meta.managed_by and f"managed_by:{meta.managed_by.lower()}" in INFERRABLE_SIGNALS:
        return FieldDifficulty.INFERRABLE
    if meta.source and f"source:{meta.source.lower()}" in INFERRABLE_SIGNALS:
        return FieldDifficulty.INFERRABLE
    if meta.derived_from:
        return FieldDifficulty.INFERRABLE
    return None


def _classify_by_name_patterns(norm: str, is_gate: bool) -> FieldDifficulty | None:
    """Match field name against universal difficulty name patterns, return match or None."""
    if any(p in norm for p in INFERRABLE_NAME_PATTERNS) and not is_gate:
        return FieldDifficulty.INFERRABLE
    if any(p in norm for p in TRIVIAL_NAME_PATTERNS):
        return FieldDifficulty.TRIVIAL
    if any(p in norm for p in PUBLIC_NAME_PATTERNS):
        return FieldDifficulty.PUBLIC
    if any(p in norm for p in OBSCURE_NAME_PATTERNS):
        return FieldDifficulty.OBSCURE
    return None


def _classify_single(meta: FieldMeta) -> FieldDifficulty:
    """Classify one field by cascading rules. Order matters."""

    # Rule 0: Explicit difficulty override from YAML (absolute priority)
    if meta.difficulty_override:
        try:
            return FieldDifficulty(meta.difficulty_override.lower())
        except ValueError:
            logger.warning(
                "invalid_difficulty_override",
                field=meta.name,
                value=meta.difficulty_override,
            )

    # Rule 1: Explicit metadata signals
    metadata_result = _classify_by_metadata_signals(meta)
    if metadata_result is not None:
        return metadata_result

    # Rule 2: Name-pattern matching (universal cross-industry patterns)
    norm = _normalise(meta.name)
    pattern_result = _classify_by_name_patterns(norm, meta.is_gate)
    if pattern_result is not None:
        return pattern_result

    # Rule 3: Type-based heuristics
    if meta.field_type in ("boolean", "bool"):
        return FieldDifficulty.FINDABLE

    # Rule 4: Low discovery_confidence = author flagged as hard
    if meta.discovery_confidence is not None:
        if meta.discovery_confidence < 0.4:
            return FieldDifficulty.OBSCURE
        if meta.discovery_confidence < 0.7:
            return FieldDifficulty.FINDABLE

    # Default
    return FieldDifficulty.FINDABLE


def classify(domain_spec: dict[str, Any]) -> dict[str, FieldDifficulty]:
    """Auto-classify all fields in a domain YAML → FieldDifficulty map."""
    fields = extract_field_meta(domain_spec)
    result: dict[str, FieldDifficulty] = {}

    for meta in fields:
        result[meta.name] = _classify_single(meta)
        logger.debug(
            "field_classified",
            field=meta.name,
            difficulty=result[meta.name].value,
            managed_by=meta.managed_by,
            is_gate=meta.is_gate,
        )

    counts: dict[str, int] = {}
    for d in result.values():
        counts[d.value] = counts.get(d.value, 0) + 1
    logger.info(
        "field_classifier.complete",
        domain=spec_domain_name(domain_spec),
        total_fields=len(result),
        distribution=counts,
    )
    return result


# ──────────────────────────────────────────────
# Optional: LLM-assisted refinement (one-time)
# ──────────────────────────────────────────────


def build_calibration_prompt(
    domain_name: str,
    fields: list[FieldMeta],
    heuristic_map: dict[str, FieldDifficulty],
) -> dict[str, str]:
    """Build a Sonar prompt to validate/refine the heuristic classification.
    Run once per domain, cache the result forever.
    Uses sonar (base) with disable_search — ~$0.001.
    """
    field_lines = []
    for f in fields:
        current = heuristic_map.get(f.name, FieldDifficulty.FINDABLE)
        parts = [f"  {f.name}: {current.value}  # type={f.field_type}"]
        if f.is_gate:
            parts.append(" gate=true")
        if f.is_scoring:
            parts.append(" scoring=true")
        field_lines.append("".join(parts))

    system = f"""You are a data acquisition specialist for the {domain_name} industry.
You understand what company/facility data is publicly available vs requires
deep research.

Field difficulty levels:
- trivial: Basic contact/identity info, always available
- public: In business databases (D&B, SEC, LinkedIn, ZoomInfo)
- findable: On company websites, industry directories, trade publications
- obscure: Requires permits, niche databases, press releases, deep search
- inferrable: Can be computed/derived from other fields, never needs search

Review the classification below and correct any misclassified fields.
Return ONLY a JSON object with field names as keys and corrected difficulty
as values. Only include fields you want to CHANGE. Empty object {{}} if all correct."""

    user = f"""Domain: {domain_name}

Current classification:
{chr(10).join(field_lines)}

Which fields are misclassified? Consider what data sources exist
for {domain_name} companies specifically."""

    return {"system": system, "user": user}


def apply_calibration(
    base_map: dict[str, FieldDifficulty],
    overrides: dict[str, str],
) -> dict[str, FieldDifficulty]:
    """Merge LLM calibration overrides into the heuristic map."""
    result = dict(base_map)
    for field_name, difficulty_str in overrides.items():
        try:
            result[field_name] = FieldDifficulty(difficulty_str)
            logger.info(
                "calibration_override",
                field=field_name,
                old=base_map.get(field_name, "?"),
                new=difficulty_str,
            )
        except ValueError:
            logger.warning(
                "calibration_invalid",
                field=field_name,
                value=difficulty_str,
            )
    return result


# ──────────────────────────────────────────────
# Domain filter resolver — reads from YAML only
# ──────────────────────────────────────────────

# Universal fallback sources (applies to any domain)
_DEFAULT_PUBLIC_SOURCES: list[str] = [
    "dnb.com",
    "zoominfo.com",
    "linkedin.com",
    "sec.gov",
    "sam.gov",
    "opencorporates.com",
]


def resolve_domain_filters(
    domain_spec: dict[str, Any],
    difficulty: FieldDifficulty,
) -> list[str]:
    """Resolve search domain filters from the YAML search_sources block.

    Falls back to universal public sources if YAML doesn't specify.
    OBSCURE always returns [] (broadest search).
    TRIVIAL always returns [] (no search needed).
    INFERRABLE always returns [] (no search needed).
    """
    if difficulty in (
        FieldDifficulty.OBSCURE,
        FieldDifficulty.TRIVIAL,
        FieldDifficulty.INFERRABLE,
    ):
        return []

    # Read from YAML: search_sources.{difficulty} = [domains]
    sources = domain_spec.get("search_sources", {})
    if isinstance(sources, dict):
        explicit = sources.get(difficulty.value, [])
        if explicit:
            return list(explicit)

    # Fallback: universal public sources for PUBLIC difficulty
    if difficulty == FieldDifficulty.PUBLIC:
        return list(_DEFAULT_PUBLIC_SOURCES)

    return []


# ──────────────────────────────────────────────
# Full auto-pipeline: YAML → classified + optimized
# ──────────────────────────────────────────────


@dataclass
class DomainClassification:
    """Complete classification result for a domain."""

    domain: str
    field_map: dict[str, FieldDifficulty]
    domain_filters: dict[str, list[str]]
    gate_fields: set[str]
    scoring_fields: set[str]
    time_sensitive_fields: set[str]
    ambiguous_fields: set[str]
    stats: dict[str, int]
    calibrated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize for TransportPacket.metadata observability."""
        return {
            "domain": self.domain,
            "field_map": {k: v.value for k, v in self.field_map.items()},
            "domain_filters": self.domain_filters,
            "gate_fields": sorted(self.gate_fields),
            "scoring_fields": sorted(self.scoring_fields),
            "time_sensitive_fields": sorted(self.time_sensitive_fields),
            "ambiguous_fields": sorted(self.ambiguous_fields),
            "stats": self.stats,
            "calibrated": self.calibrated,
        }


def auto_classify_domain(
    domain_spec: dict[str, Any],
) -> DomainClassification:
    """One-call pipeline: domain YAML → fully classified field map + filters.

    Zero LLM cost. Everything read from the YAML. No external arguments.
    Feed result directly into search_optimizer_v2.
    """
    domain_name = spec_domain_name(domain_spec)

    field_map = classify(domain_spec)

    # Build domain filter map from YAML search_sources
    domain_filters: dict[str, list[str]] = {}
    for diff in FieldDifficulty:
        filters = resolve_domain_filters(domain_spec, diff)
        if filters:
            domain_filters[diff.value] = filters

    # Stats
    stats: dict[str, int] = {}
    for d in field_map.values():
        stats[d.value] = stats.get(d.value, 0) + 1

    return DomainClassification(
        domain=domain_name,
        field_map=field_map,
        domain_filters=domain_filters,
        gate_fields=_read_set(domain_spec, "gate_fields"),
        scoring_fields=_read_set(domain_spec, "scoring_fields"),
        time_sensitive_fields=_read_set(domain_spec, "time_sensitive_fields"),
        ambiguous_fields=_read_set(domain_spec, "ambiguous_fields"),
        stats=stats,
    )


# ──────────────────────────────────────────────
# YAML Contract (domain_spec.yaml schema)
# ──────────────────────────────────────────────
#
# domain: "plastics_recycling"          # required
# industry: "plastics"                  # optional (for calibration prompts)
#
# gate_fields:                          # optional — list of field names
#   - polymers_handled
#   - facility_type
#
# scoring_fields:                       # optional
#   - annual_capacity_tons
#   - certifications
#
# time_sensitive_fields:                # optional — triggers recency filter
#   - recent_news
#   - annual_revenue_usd
#   - employee_count
#
# ambiguous_fields:                     # optional — triggers reasoning model
#   - facility_type
#   - ownership_type
#
# search_sources:                       # optional — domain filters by difficulty
#   public:
#     - dnb.com
#     - sec.gov
#     - zoominfo.com
#   findable:
#     - thomasnet.com
#     - plasticsrecycling.org
#     - plasticsnews.com
#
# ontology:
#   nodes:
#     Facility:
#       properties:
#         company_legal_name:
#           type: string
#           # → auto-classified TRIVIAL by name pattern
#         annual_capacity_tons:
#           type: float
#           discovery_confidence: 0.3
#           # → auto-classified OBSCURE by confidence + name
#         material_grade:
#           type: string
#           managed_by: computed
#           derived_from: [polymers_handled, certifications]
#           # → auto-classified INFERRABLE by metadata
#         contamination_tolerance:
#           type: string
#           difficulty: obscure
#           # → explicit override, no heuristic needed
