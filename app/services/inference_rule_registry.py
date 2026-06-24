"""
Inference rule registry for the ENRICH convergence loop.

Provides a decorator-based registration pattern for inference functions that
derive field values from existing entity data. Rules are executed during
convergence passes to fill gaps in entity profiles.

Usage:
    @register_inference_rule("infer_company_size_tier")
    def infer_company_size_tier(entity: dict, ctx: InferenceContext) -> InferenceResult | None:
        ...

    # Execute a rule
    result = execute_rule("infer_company_size_tier", entity, context)
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class InferenceContext:
    """Runtime context passed to every inference function."""

    tenant_id: str
    domain_id: str
    pass_number: int
    known_fields: dict[str, Any] = field(default_factory=dict)
    domain_kb: dict[str, Any] = field(default_factory=dict)
    confidence_floor: float = 0.55


@dataclass
class InferenceResult:
    """Output of a single inference function execution."""

    field_name: str
    value: Any
    confidence: float
    rule_name: str
    provenance: str = "inference"
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field_name,
            "value": self.value,
            "confidence": self.confidence,
            "rule": self.rule_name,
            "provenance": self.provenance,
            "rationale": self.rationale,
        }


InferenceFn = Callable[[dict[str, Any], InferenceContext], InferenceResult | None]

_RULE_REGISTRY: dict[str, InferenceFn] = {}


def register_inference_rule(name: str) -> Callable[[InferenceFn], InferenceFn]:
    """Decorator that registers an inference function under `name`."""

    def decorator(fn: InferenceFn) -> InferenceFn:
        if name in _RULE_REGISTRY:
            raise ValueError(f"Inference rule '{name}' is already registered")
        _RULE_REGISTRY[name] = fn
        logger.debug("Registered inference rule: %s → %s", name, fn.__qualname__)
        return fn

    return decorator


def get_rule(name: str) -> InferenceFn:
    """Return a registered rule or raise KeyError with a clear message."""
    if name not in _RULE_REGISTRY:
        available = sorted(_RULE_REGISTRY.keys())
        raise KeyError(
            f"Inference rule '{name}' not found in registry. Available rules: {available}"
        )
    return _RULE_REGISTRY[name]


def execute_rule(
    rule_name: str,
    entity: dict[str, Any],
    context: InferenceContext,
) -> InferenceResult | None:
    """
    Execute a registered inference rule. Returns None if the rule
    returns None (no confident inference). Raises KeyError if rule
    is not registered.
    """
    fn = get_rule(rule_name)
    try:
        result = fn(entity, context)
    except Exception:
        logger.exception("Inference rule '%s' raised during execution", rule_name)
        return None
    if result is not None and result.confidence < context.confidence_floor:
        logger.debug(
            "Rule '%s' produced confidence %.3f below floor %.3f — suppressing",
            rule_name,
            result.confidence,
            context.confidence_floor,
        )
        return None
    return result


# ===========================================================================
# PRODUCTION INFERENCE RULES
# ===========================================================================


@register_inference_rule("infer_company_size_tier")
def infer_company_size_tier(entity: dict, ctx: InferenceContext) -> InferenceResult | None:
    """Infer company_size_tier from employee_count and revenue fields."""
    emp = entity.get("employee_count") or entity.get("employees")
    revenue = entity.get("annual_revenue_usd") or entity.get("revenue")
    if emp is None and revenue is None:
        return None
    try:
        emp_n = int(emp) if emp is not None else None
        rev_n = float(revenue) if revenue is not None else None
    except (TypeError, ValueError):
        return None
    if emp_n is not None:
        if emp_n < 10:
            tier, conf = "micro", 0.90
        elif emp_n < 50:
            tier, conf = "small", 0.88
        elif emp_n < 250:
            tier, conf = "mid_market", 0.85
        elif emp_n < 1000:
            tier, conf = "enterprise", 0.83
        else:
            tier, conf = "large_enterprise", 0.88
        return InferenceResult(
            field_name="company_size_tier",
            value=tier,
            confidence=conf,
            rule_name="infer_company_size_tier",
            rationale=f"employee_count={emp_n}",
        )
    if rev_n is not None:
        if rev_n < 1_000_000:
            tier, conf = "micro", 0.70
        elif rev_n < 10_000_000:
            tier, conf = "small", 0.68
        elif rev_n < 100_000_000:
            tier, conf = "mid_market", 0.65
        else:
            tier, conf = "enterprise", 0.65
        return InferenceResult(
            field_name="company_size_tier",
            value=tier,
            confidence=conf,
            rule_name="infer_company_size_tier",
            rationale=f"annual_revenue={rev_n} (fallback)",
        )
    return None


@register_inference_rule("infer_email_domain_from_website")
def infer_email_domain_from_website(entity: dict, ctx: InferenceContext) -> InferenceResult | None:
    """Infer corporate email domain from website URL."""
    website = entity.get("website") or entity.get("website_url")
    if not website:
        return None
    domain = re.sub(r"^https?://", "", str(website).strip().lower())
    domain = re.sub(r"^www\.", "", domain)
    domain = domain.split("/")[0].split("?")[0]
    if not domain or "." not in domain:
        return None
    return InferenceResult(
        field_name="email_domain",
        value=domain,
        confidence=0.92,
        rule_name="infer_email_domain_from_website",
        rationale=f"derived from website={website}",
    )


@register_inference_rule("infer_geography_from_postal_code")
def infer_geography_from_postal_code(entity: dict, ctx: InferenceContext) -> InferenceResult | None:
    """Infer region/country from postal code prefix."""
    postal = entity.get("postal_code") or entity.get("zip_code")
    if not postal:
        return None
    postal_str = str(postal).strip().upper()
    if re.match(r"^\d{5}(-\d{4})?$", postal_str):
        prefix = int(postal_str[:5])
        if prefix < 20000:
            region = "Northeast"
        elif prefix < 30000:
            region = "Southeast"
        elif prefix < 50000:
            region = "Midwest"
        elif prefix < 80000:
            region = "South"
        else:
            region = "West"
        return InferenceResult(
            field_name="region",
            value=region,
            confidence=0.82,
            rule_name="infer_geography_from_postal_code",
            rationale=f"US zip={postal_str}",
        )
    return None


@register_inference_rule("infer_facility_tier_from_capacity")
def infer_facility_tier_from_capacity(
    entity: dict, ctx: InferenceContext
) -> InferenceResult | None:
    """Plastics vertical: infer facility_tier from processing_capacity_tons_per_year."""
    capacity = entity.get("processing_capacity_tons_per_year") or entity.get("annual_capacity_tons")
    if capacity is None:
        return None
    try:
        cap_n = float(capacity)
    except (TypeError, ValueError):
        return None
    kb = ctx.domain_kb.get("facility_tier_thresholds", {})
    micro_max = kb.get("micro_max", 500)
    small_max = kb.get("small_max", 5_000)
    mid_max = kb.get("mid_max", 25_000)
    if cap_n <= micro_max:
        tier, conf = "micro", 0.87
    elif cap_n <= small_max:
        tier, conf = "small", 0.85
    elif cap_n <= mid_max:
        tier, conf = "mid", 0.83
    else:
        tier, conf = "large", 0.88
    return InferenceResult(
        field_name="facility_tier",
        value=tier,
        confidence=conf,
        rule_name="infer_facility_tier_from_capacity",
        rationale=f"capacity={cap_n} tons/year",
    )


@register_inference_rule("infer_material_grade_from_mfi")
def infer_material_grade_from_mfi(entity: dict, ctx: InferenceContext) -> InferenceResult | None:
    """Plastics vertical: infer material_grade from melt_flow_index (MFI g/10min)."""
    mfi = entity.get("melt_flow_index") or entity.get("mfi")
    material = entity.get("material_type", "HDPE").upper()
    if mfi is None:
        return None
    try:
        mfi_n = float(mfi)
    except (TypeError, ValueError):
        return None
    kb = ctx.domain_kb.get("mfi_grade_map", {})
    if material in ("HDPE", "PE"):
        grade_map = kb.get(
            "HDPE",
            [
                (0.5, "HD_pipe", 0.88),
                (2.0, "HD_blow", 0.85),
                (8.0, "HD_injection", 0.83),
                (float("inf"), "HD_fiber", 0.80),
            ],
        )
    else:
        grade_map = kb.get(material, [(float("inf"), "generic", 0.60)])
    for threshold, grade, conf in grade_map:
        if mfi_n <= threshold:
            return InferenceResult(
                field_name="material_grade",
                value=grade,
                confidence=conf,
                rule_name="infer_material_grade_from_mfi",
                rationale=f"MFI={mfi_n} material={material}",
            )
    return None


@register_inference_rule("infer_contamination_tolerance")
def infer_contamination_tolerance(entity: dict, ctx: InferenceContext) -> InferenceResult | None:
    """Plastics vertical: infer contamination_tolerance from facility_tier and material_grade."""
    tier = entity.get("facility_tier")
    grade = entity.get("material_grade")
    if not tier or not grade:
        return None
    high_tolerance_tiers = {"micro", "small"}
    low_grade_patterns = {"fiber", "generic"}
    tolerance = (
        "high"
        if (
            tier in high_tolerance_tiers or any(p in str(grade).lower() for p in low_grade_patterns)
        )
        else "low"
    )
    conf = 0.78 if tolerance == "high" else 0.72
    return InferenceResult(
        field_name="contamination_tolerance",
        value=tolerance,
        confidence=conf,
        rule_name="infer_contamination_tolerance",
        rationale=f"facility_tier={tier} material_grade={grade}",
    )


@register_inference_rule("infer_icp_fit_score")
def infer_icp_fit_score(entity: dict, ctx: InferenceContext) -> InferenceResult | None:
    """Generic ICP fit scoring: weighted sum of known firmographic signals."""
    score = 0.0
    factors = 0
    if entity.get("company_size_tier") in ("mid_market", "enterprise", "large_enterprise"):
        score += 0.30
        factors += 1
    rev = entity.get("annual_revenue_usd")
    if rev:
        try:
            if float(rev) > 1_000_000:
                score += 0.25
                factors += 1
        except (TypeError, ValueError):
            pass
    if entity.get("email_domain"):
        score += 0.10
        factors += 1
    if entity.get("region"):
        score += 0.10
        factors += 1
    if entity.get("facility_tier") in ("mid", "large"):
        score += 0.25
        factors += 1
    if factors == 0:
        return None
    conf = min(0.55 + (factors * 0.06), 0.90)
    return InferenceResult(
        field_name="icp_fit_score",
        value=round(score, 4),
        confidence=conf,
        rule_name="infer_icp_fit_score",
        rationale=f"{factors} contributing factors",
    )


@register_inference_rule("infer_buyer_persona")
def infer_buyer_persona(entity: dict, ctx: InferenceContext) -> InferenceResult | None:
    """Infer buyer_persona from job_title if present."""
    title = entity.get("job_title") or entity.get("title") or ""
    title_lower = str(title).lower()
    if not title_lower:
        return None
    exec_keywords = {"ceo", "coo", "president", "owner", "founder", "vp", "svp", "evp"}
    ops_keywords = {"operations", "ops", "plant", "facility", "production", "supply"}
    proc_keywords = {"procurement", "purchasing", "buyer", "sourcing"}
    tech_keywords = {"engineer", "technical", "r&d", "research", "quality"}
    for kw in exec_keywords:
        if kw in title_lower:
            return InferenceResult(
                "buyer_persona",
                "executive",
                0.85,
                "infer_buyer_persona",
                rationale=f"title={title}",
            )
    for kw in proc_keywords:
        if kw in title_lower:
            return InferenceResult(
                "buyer_persona",
                "procurement",
                0.83,
                "infer_buyer_persona",
                rationale=f"title={title}",
            )
    for kw in ops_keywords:
        if kw in title_lower:
            return InferenceResult(
                "buyer_persona",
                "operations",
                0.80,
                "infer_buyer_persona",
                rationale=f"title={title}",
            )
    for kw in tech_keywords:
        if kw in title_lower:
            return InferenceResult(
                "buyer_persona",
                "technical",
                0.78,
                "infer_buyer_persona",
                rationale=f"title={title}",
            )
    return InferenceResult(
        "buyer_persona",
        "unknown",
        0.55,
        "infer_buyer_persona",
        rationale=f"title={title} — no match",
    )


def load_domain_rules(domain_kb: dict[str, Any]) -> int:
    """
    Load domain-specific inference rules from domain KB.
    Returns count of rules registered.
    """
    rules_spec = domain_kb.get("inference_rules", [])
    registered = 0
    for spec in rules_spec:
        rule_name = spec.get("name")
        if not rule_name:
            continue
        if rule_name in _RULE_REGISTRY:
            continue
        _register_condition_rule(rule_name, spec)
        registered += 1
    logger.info("Loaded %d domain-specific inference rules from KB", registered)
    return registered


def _register_condition_rule(rule_name: str, spec: dict[str, Any]) -> None:
    """Auto-generate and register a simple condition-based inference rule."""
    target_field = spec["field"]
    conditions = spec.get("conditions", [])
    default_value = spec.get("value")
    confidence = float(spec.get("confidence", 0.65))

    def _rule(entity: dict, ctx: InferenceContext) -> InferenceResult | None:
        for cond in conditions:
            src_field = cond.get("source_field")
            operator = cond.get("operator", "eq")
            cond_value = cond.get("value")
            entity_val = entity.get(src_field)
            if entity_val is None:
                return None
            match operator:
                case "eq":
                    if entity_val != cond_value:
                        return None
                case "gt":
                    try:
                        if float(entity_val) <= float(cond_value):
                            return None
                    except (TypeError, ValueError):
                        return None
                case "lt":
                    try:
                        if float(entity_val) >= float(cond_value):
                            return None
                    except (TypeError, ValueError):
                        return None
                case "contains":
                    if str(cond_value).lower() not in str(entity_val).lower():
                        return None
                case _:
                    return None
        output_value = spec.get("output_value", default_value)
        if output_value is None:
            return None
        return InferenceResult(
            field_name=target_field,
            value=output_value,
            confidence=confidence,
            rule_name=rule_name,
            provenance="domain_kb",
            rationale="condition rule from KB",
        )

    _RULE_REGISTRY[rule_name] = _rule
    logger.debug("Auto-registered condition rule: %s → %s", rule_name, target_field)


def list_registered_rules() -> list[str]:
    return sorted(_RULE_REGISTRY.keys())
