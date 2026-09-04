from __future__ import annotations

import pytest

from app.services.action_authority import (
    ActionAuthorizationError,
    authorize_action,
    evaluate_writeback_payload,
    get_action_policy,
    get_tool_policy,
)
from app.services.dependency_enforcement import (
    ActionDependencyError,
    assert_action_dependencies,
    evaluate_action_dependencies,
)

pytestmark = [pytest.mark.unit, pytest.mark.enforcement, pytest.mark.authority]


def ready_attestation() -> dict[str, object]:
    return {
        "dependency_readiness": {
            "PerplexitySONAR": {"required": True, "ready": True, "env_vars": [], "missing_env": []},
            "Redis": {"required": False, "ready": True, "env_vars": [], "missing_env": []},
            "PostgreSQL": {"required": False, "ready": True, "env_vars": [], "missing_env": []},
            "ConstellationGate": {
                "required": True,
                "ready": True,
                "env_vars": [],
                "missing_env": [],
            },
            "OdooCRM": {"required": False, "ready": False, "env_vars": [], "missing_env": []},
            "SalesforceCRM": {"required": False, "ready": True, "env_vars": [], "missing_env": []},
            "HubSpotCRM": {"required": False, "ready": False, "env_vars": [], "missing_env": []},
            "OpenAI": {"required": False, "ready": False, "env_vars": [], "missing_env": []},
            "Anthropic": {"required": False, "ready": False, "env_vars": [], "missing_env": []},
        }
    }


def blocked_writeback_attestation() -> dict[str, object]:
    return {
        "dependency_readiness": {
            "PerplexitySONAR": {"required": True, "ready": True, "env_vars": [], "missing_env": []},
            "Redis": {"required": False, "ready": True, "env_vars": [], "missing_env": []},
            "PostgreSQL": {"required": False, "ready": True, "env_vars": [], "missing_env": []},
            "ConstellationGate": {
                "required": True,
                "ready": True,
                "env_vars": [],
                "missing_env": [],
            },
            "OdooCRM": {"required": False, "ready": False, "env_vars": [], "missing_env": []},
            "SalesforceCRM": {"required": False, "ready": False, "env_vars": [], "missing_env": []},
            "HubSpotCRM": {"required": False, "ready": False, "env_vars": [], "missing_env": []},
            "OpenAI": {"required": False, "ready": False, "env_vars": [], "missing_env": []},
            "Anthropic": {"required": False, "ready": False, "env_vars": [], "missing_env": []},
        }
    }


def test_action_and_tool_policies_expose_current_constitution() -> None:
    assert get_action_policy("writeback")["mutation_class"] == "external_mutation"
    assert get_tool_policy("writeback")["chassis_action"] == "writeback"


def test_evaluate_action_dependencies_allows_enrich_with_ready_required_deps() -> None:
    evaluation = evaluate_action_dependencies("enrich", attestation=ready_attestation())
    assert evaluation["allowed"] is True
    assert evaluation["missing_required"] == []


def test_assert_action_dependencies_blocks_writeback_without_any_target_integration() -> None:
    with pytest.raises(ActionDependencyError, match="no_target_integration_ready"):
        assert_action_dependencies("writeback", attestation=blocked_writeback_attestation())


def test_evaluate_writeback_payload_skips_low_confidence_fields() -> None:
    result = evaluate_writeback_payload(
        {
            "crm_type": "salesforce",
            "object_type": "Account",
            "record_id": "001ABC",
            "enriched_data": {"recycling_grade": "HDPE", "annual_tonnage": 50000},
            "confidence_threshold": 0.90,
            "_field_confidences": {"recycling_grade": 0.42, "annual_tonnage": 0.95},
        }
    )
    assert result["status"] == "partial"
    assert "recycling_grade" in result["skipped_fields"]
    assert "annual_tonnage" in result["written_fields"]


def test_authorize_action_requires_policy_clearance_for_writeback() -> None:
    with pytest.raises(ActionAuthorizationError, match="requires explicit policy clearance"):
        authorize_action(
            "writeback",
            payload={
                "crm_type": "salesforce",
                "object_type": "Account",
                "record_id": "001ABC",
                "enriched_data": {"industry": "Manufacturing"},
                "_field_confidences": {"industry": 0.95},
            },
            policy_cleared=False,
            attestation=ready_attestation(),
        )


def test_authorize_action_returns_writeback_evaluation_when_cleared() -> None:
    result = authorize_action(
        "writeback",
        payload={
            "crm_type": "salesforce",
            "object_type": "Account",
            "record_id": "001ABC",
            "enriched_data": {"industry": "Manufacturing"},
            "_field_confidences": {"industry": 0.95},
        },
        policy_cleared=True,
        attestation=ready_attestation(),
    )
    assert result["writeback_evaluation"]["status"] == "completed"
