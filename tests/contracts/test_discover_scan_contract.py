"""
Contract tests for the /api/v1/scan endpoint (app/api/v1/discover.py).

The endpoint was previously broken (SonarCloud python:S930): it ``await``-ed the
*synchronous* ``scan_crm_fields`` service with mismatched keyword arguments and
raw dict payloads, so every call raised at runtime. These assertions pin the
service contract the endpoint depends on so the defect cannot silently return.

Source: app/services/crm_field_scanner.py, app/api/v1/discover.py
Markers: unit
"""

from __future__ import annotations

import inspect

import pytest

from app.services.crm_field_scanner import (
    CRMField,
    scan_crm_fields,
    scan_result_to_dict,
)


@pytest.mark.unit
def test_scan_crm_fields_is_synchronous() -> None:
    """The endpoint must call this synchronously — it is not a coroutine."""
    assert not inspect.iscoroutinefunction(scan_crm_fields)


@pytest.mark.unit
def test_scan_crm_fields_positional_contract() -> None:
    """Signature is (crm_fields, domain_spec) — not the 4 kwargs the bug passed."""
    params = list(inspect.signature(scan_crm_fields).parameters)
    assert params[:2] == ["crm_fields", "domain_spec"]


@pytest.mark.unit
def test_crm_field_constructor_contract() -> None:
    """The endpoint builds CRMField objects with these exact fields."""
    field = CRMField(
        name="company_name",
        field_type="string",
        sample_values=["Acme"],
        fill_rate=0.9,
    )
    assert field.name == "company_name"
    assert field.field_type == "string"


@pytest.mark.unit
def test_scan_result_is_dict_serializable() -> None:
    """The endpoint returns scan_result_to_dict(...), which must be a mapping."""
    result = scan_crm_fields([], {"domain": {"id": "t", "version": "0.0.0"}})
    assert isinstance(scan_result_to_dict(result), dict)


# ── Source provenance contract (crm-schema-scan pack, PR-A) ─────────────────

_PROVENANCE_DOMAIN = {
    "domain": {"id": "t", "version": "0.0.0"},
    "ontology": {"nodes": [{"label": "Partner", "properties": {"phone": {"type": "string"}}}]},
}


@pytest.mark.unit
def test_crm_field_provenance_is_optional_and_defaults_to_none() -> None:
    """Legacy callers construct CRMField without provenance; new callers may supply it."""
    legacy = CRMField(name="phone", field_type="char")
    assert legacy.source_system is None
    assert legacy.source_resource is None
    sourced = CRMField(
        name="phone", field_type="char", source_system="odoo", source_resource="crm.lead"
    )
    assert (sourced.source_system, sourced.source_resource) == ("odoo", "crm.lead")


@pytest.mark.unit
def test_serialized_mappings_carry_provenance_keys() -> None:
    """matched/unmapped entries expose source_system + source_resource; missing entries never do."""
    fields = [
        CRMField(
            name="phone", field_type="char", source_system="odoo", source_resource="res.partner"
        ),
        CRMField(name="phone", field_type="char", source_system="odoo", source_resource="crm.lead"),
        CRMField(
            name="x_notes", field_type="text", source_system="odoo", source_resource="crm.lead"
        ),
    ]
    data = scan_result_to_dict(scan_crm_fields(fields, _PROVENANCE_DOMAIN))
    assert {m["source_resource"] for m in data["matched"]} == {"res.partner", "crm.lead"}
    assert all(m["source_system"] == "odoo" for m in data["matched"])
    assert data["unmapped"] == [
        {"crm_field": "x_notes", "source_system": "odoo", "source_resource": "crm.lead"}
    ]
    assert data["missing"] == []
    # Two provenance-bearing mappings cover one domain property exactly once.
    assert data["matched_count"] == 2
    assert data["coverage_ratio"] == 1.0
