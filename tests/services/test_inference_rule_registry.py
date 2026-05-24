"""Tests for inference rule registry."""
import pytest

from app.services.inference_rule_registry import (
    InferenceContext,
    InferenceResult,
    execute_rule,
    get_rule,
    list_registered_rules,
    load_domain_rules,
)


@pytest.fixture
def context():
    """Standard inference context for tests."""
    return InferenceContext(
        tenant_id="t1",
        domain_id="plastics",
        pass_number=1,
        known_fields={},
        domain_kb={},
        confidence_floor=0.55,
    )


def test_builtin_rules_registered():
    """Built-in inference rules should be registered at import time."""
    rules = list_registered_rules()
    assert "infer_company_size_tier" in rules
    assert "infer_email_domain_from_website" in rules
    assert "infer_geography_from_postal_code" in rules
    assert "infer_facility_tier_from_capacity" in rules
    assert "infer_icp_fit_score" in rules


def test_get_rule_returns_function():
    """get_rule should return the registered function."""
    fn = get_rule("infer_company_size_tier")
    assert callable(fn)


def test_get_rule_raises_for_unknown():
    """get_rule should raise KeyError for unknown rules."""
    with pytest.raises(KeyError, match="not found"):
        get_rule("nonexistent_rule")


def test_infer_company_size_tier_from_employees(context):
    """infer_company_size_tier should work with employee_count."""
    entity = {"employee_count": 150}
    result = execute_rule("infer_company_size_tier", entity, context)
    assert result is not None
    assert result.field_name == "company_size_tier"
    assert result.value == "mid_market"
    assert result.confidence >= 0.80


def test_infer_company_size_tier_from_revenue(context):
    """infer_company_size_tier should fall back to revenue."""
    entity = {"annual_revenue_usd": 5_000_000}
    result = execute_rule("infer_company_size_tier", entity, context)
    assert result is not None
    assert result.field_name == "company_size_tier"
    assert result.value == "small"


def test_infer_email_domain_from_website(context):
    """infer_email_domain_from_website should extract domain."""
    entity = {"website": "https://www.example.com/about"}
    result = execute_rule("infer_email_domain_from_website", entity, context)
    assert result is not None
    assert result.field_name == "email_domain"
    assert result.value == "example.com"


def test_infer_geography_from_us_zip(context):
    """infer_geography_from_postal_code should work with US zips."""
    entity = {"postal_code": "90210"}
    result = execute_rule("infer_geography_from_postal_code", entity, context)
    assert result is not None
    assert result.field_name == "region"
    assert result.value == "West"


def test_infer_facility_tier_from_capacity(context):
    """infer_facility_tier_from_capacity should categorize by tons/year."""
    entity = {"processing_capacity_tons_per_year": 10000}
    result = execute_rule("infer_facility_tier_from_capacity", entity, context)
    assert result is not None
    assert result.field_name == "facility_tier"
    assert result.value == "mid"


def test_infer_icp_fit_score(context):
    """infer_icp_fit_score should compute weighted score."""
    entity = {
        "company_size_tier": "enterprise",
        "annual_revenue_usd": 50_000_000,
        "email_domain": "acme.com",
        "region": "West",
    }
    result = execute_rule("infer_icp_fit_score", entity, context)
    assert result is not None
    assert result.field_name == "icp_fit_score"
    assert 0.0 <= result.value <= 1.0


def test_infer_buyer_persona_from_title(context):
    """infer_buyer_persona should categorize by job title."""
    entity = {"job_title": "VP of Procurement"}
    result = execute_rule("infer_buyer_persona", entity, context)
    assert result is not None
    assert result.field_name == "buyer_persona"
    assert result.value == "procurement"


def test_low_confidence_filtered(context):
    """Results below confidence floor should be filtered."""
    context.confidence_floor = 0.99  # Very high floor
    entity = {"employee_count": 150}
    result = execute_rule("infer_company_size_tier", entity, context)
    # Result confidence is ~0.85, below 0.99 floor
    assert result is None


def test_missing_data_returns_none(context):
    """Missing required data should return None."""
    entity = {}  # No employee_count or revenue
    result = execute_rule("infer_company_size_tier", entity, context)
    assert result is None


def test_load_domain_rules():
    """load_domain_rules should register rules from KB."""
    domain_kb = {
        "inference_rules": [
            {
                "name": "test_custom_rule",
                "field": "custom_field",
                "conditions": [
                    {"source_field": "industry", "operator": "eq", "value": "plastics"}
                ],
                "output_value": "matched",
                "confidence": 0.75,
            }
        ]
    }
    count = load_domain_rules(domain_kb)
    assert count == 1
    assert "test_custom_rule" in list_registered_rules()


def test_inference_result_to_dict():
    """InferenceResult.to_dict should serialize correctly."""
    result = InferenceResult(
        field_name="test_field",
        value="test_value",
        confidence=0.85,
        rule_name="test_rule",
        provenance="inference",
        rationale="test rationale",
    )
    d = result.to_dict()
    assert d["field"] == "test_field"
    assert d["value"] == "test_value"
    assert d["confidence"] == 0.85
    assert d["rule"] == "test_rule"
