"""
Tests for CRM Field Scanner — validates scan, seed YAML, and discovery report.
Run: pytest tests/test_crm_field_scanner.py -v
"""

from __future__ import annotations

import pytest

from app.services.crm_field_scanner import (
    CRMField,
    ImpactTier,
    discovery_report_to_dict,
    generate_discovery_report,
    generate_seed_yaml,
    scan_crm_fields,
    scan_result_to_dict,
)

# ── Fixtures ─────────────────────────────────────────────────


PLASTICS_DOMAIN_SPEC = {
    "domain": {"id": "plastics-recycling", "name": "Plastics Recycling", "version": "8.0.0"},
    "ontology": {
        "nodes": [
            {
                "label": "Partner",
                "properties": {
                    "name": {"type": "string", "description": "Company legal name"},
                    "city": {"type": "string", "description": "City"},
                    "phone": {"type": "string", "description": "Phone number"},
                    "materials_handled": {"type": "list", "description": "Polymers processed"},
                    "contamination_tolerance_pct": {
                        "type": "float",
                        "description": "Max contamination %",
                    },
                    "process_types": {"type": "list", "description": "Processing capabilities"},
                    "min_mfi": {"type": "float", "description": "Minimum melt flow index"},
                    "max_mfi": {"type": "float", "description": "Maximum melt flow index"},
                    "certifications": {"type": "list", "description": "ISO/other certs"},
                    "facility_size_sqft": {
                        "type": "integer",
                        "description": "Facility square footage",
                    },
                    "annual_capacity_lbs": {
                        "type": "integer",
                        "description": "Annual processing capacity",
                    },
                    "material_grade": {
                        "type": "string",
                        "managed_by": "inference",
                        "description": "Inferred grade",
                    },
                    "facility_tier": {
                        "type": "string",
                        "managed_by": "inference",
                        "description": "Inferred tier",
                    },
                    "buyer_class": {
                        "type": "string",
                        "managed_by": "inference",
                        "description": "Inferred buyer class",
                    },
                },
            }
        ],
    },
    "gates": [
        {"candidate_property": "materials_handled"},
        {"candidate_property": "contamination_tolerance_pct"},
        {"candidate_property": "process_types"},
        {"candidate_property": "min_mfi"},
        {"candidate_property": "max_mfi"},
    ],
    "scoring_dimensions": [
        {"candidate_property": "certifications"},
        {"candidate_property": "facility_size_sqft"},
        {"candidate_property": "annual_capacity_lbs"},
    ],
    "inference_rules": [
        {
            "conditions": [
                {"field": "materials_handled"},
                {"field": "contamination_tolerance_pct"},
            ],
            "outputs": [{"field": "material_grade"}],
        },
        {
            "conditions": [{"field": "process_types"}],
            "outputs": [{"field": "facility_tier"}],
        },
    ],
}


RECYCLER_CRM_FIELDS = [
    CRMField(name="name", field_type="string"),
    CRMField(name="city", field_type="string"),
    CRMField(name="phone", field_type="string"),
    CRMField(name="notes", field_type="text"),
    CRMField(name="category", field_type="selection"),
]


# ── Scan Tests ───────────────────────────────────────────────


class TestScanCrmFields:
    def test_basic_scan_counts(self):
        result = scan_crm_fields(RECYCLER_CRM_FIELDS, PLASTICS_DOMAIN_SPEC)
        assert result.matched_count == 3  # name, city, phone
        assert result.unmapped_count == 2  # notes, category
        assert result.missing_count == 11  # 14 domain props - 3 matched
        assert result.total_crm_fields == 5
        assert result.total_domain_properties == 14

    def test_coverage_ratio(self):
        result = scan_crm_fields(RECYCLER_CRM_FIELDS, PLASTICS_DOMAIN_SPEC)
        assert 0.2 < result.coverage_ratio < 0.25  # 3/14

    def test_gate_critical_missing(self):
        result = scan_crm_fields(RECYCLER_CRM_FIELDS, PLASTICS_DOMAIN_SPEC)
        gate_missing = result.gate_critical_missing
        assert len(gate_missing) == 5
        gate_names = {m.domain_property for m in gate_missing}
        assert "materials_handled" in gate_names
        assert "contamination_tolerance_pct" in gate_names

    def test_missing_sorted_by_impact(self):
        result = scan_crm_fields(RECYCLER_CRM_FIELDS, PLASTICS_DOMAIN_SPEC)
        tiers = [m.impact_tier for m in result.missing]
        tier_order = [
            ImpactTier.GATE_CRITICAL,
            ImpactTier.SCORING_CRITICAL,
            ImpactTier.INFERENCE_INPUT,
            ImpactTier.ENRICHABLE,
            ImpactTier.NICE_TO_HAVE,
        ]
        last_idx = -1
        for t in tiers:
            idx = tier_order.index(t) if t in tier_order else 99
            assert idx >= last_idx
            last_idx = idx

    def test_prefix_normalization(self):
        crm = [CRMField(name="x_materials_handled", field_type="list")]
        result = scan_crm_fields(crm, PLASTICS_DOMAIN_SPEC)
        assert result.matched_count == 1

    def test_scan_hash_deterministic(self):
        r1 = scan_crm_fields(RECYCLER_CRM_FIELDS, PLASTICS_DOMAIN_SPEC)
        r2 = scan_crm_fields(RECYCLER_CRM_FIELDS, PLASTICS_DOMAIN_SPEC)
        assert r1.scan_hash == r2.scan_hash

    def test_empty_crm_fields(self):
        result = scan_crm_fields([], PLASTICS_DOMAIN_SPEC)
        assert result.matched_count == 0
        assert result.missing_count == 14
        assert result.coverage_ratio == pytest.approx(0.0)


# ── Seed YAML Tests ──────────────────────────────────────────


class TestSeedYaml:
    def test_seed_version(self):
        result = scan_crm_fields(RECYCLER_CRM_FIELDS, PLASTICS_DOMAIN_SPEC)
        seed = generate_seed_yaml(result, PLASTICS_DOMAIN_SPEC)
        assert seed["domain"]["version"] == "0.1.0-seed"

    def test_seed_contains_only_matched(self):
        result = scan_crm_fields(RECYCLER_CRM_FIELDS, PLASTICS_DOMAIN_SPEC)
        seed = generate_seed_yaml(result, PLASTICS_DOMAIN_SPEC)
        props = seed["ontology"]["nodes"][0]["properties"]
        assert len(props) == 3  # name, city, phone


# ── Discovery Report Tests ───────────────────────────────────


class TestDiscoveryReport:
    def test_report_counts(self):
        result = scan_crm_fields(RECYCLER_CRM_FIELDS, PLASTICS_DOMAIN_SPEC)
        report = generate_discovery_report(result, PLASTICS_DOMAIN_SPEC, entity_count=100)
        assert report.gate_blocked_count == 5
        assert report.scoring_degraded_count == 3
        assert report.customer_fields == 5
        assert report.domain_fields == 14
        assert len(report.missing_entries) == 11

    def test_cost_estimation(self):
        result = scan_crm_fields(RECYCLER_CRM_FIELDS, PLASTICS_DOMAIN_SPEC)
        report = generate_discovery_report(result, PLASTICS_DOMAIN_SPEC, entity_count=5000)
        assert report.estimated_enrichment_cost_usd > 0

    def test_inference_fields_tagged_correctly(self):
        result = scan_crm_fields(RECYCLER_CRM_FIELDS, PLASTICS_DOMAIN_SPEC)
        report = generate_discovery_report(result, PLASTICS_DOMAIN_SPEC)
        inference_entries = [
            e for e in report.missing_entries if e.acquisition_method == "inference"
        ]
        assert len(inference_entries) >= 2  # material_grade, facility_tier, buyer_class

    def test_serialization(self):
        result = scan_crm_fields(RECYCLER_CRM_FIELDS, PLASTICS_DOMAIN_SPEC)
        report = generate_discovery_report(result, PLASTICS_DOMAIN_SPEC)
        d = discovery_report_to_dict(report)
        assert "summary" in d
        assert "headline" in d["summary"]
        assert "missing_fields" in d

    def test_scan_result_serialization(self):
        result = scan_crm_fields(RECYCLER_CRM_FIELDS, PLASTICS_DOMAIN_SPEC)
        d = scan_result_to_dict(result)
        assert d["matched_count"] == 3
        assert len(d["missing"]) == 11


# ── Source Provenance Tests ──────────────────────────────────


class TestSourceProvenance:
    ODOO_FIELDS = [
        CRMField(
            name="phone", field_type="char", source_system="odoo", source_resource="res.partner"
        ),
        CRMField(name="phone", field_type="char", source_system="odoo", source_resource="crm.lead"),
        CRMField(
            name="x_custom_material_notes",
            field_type="text",
            source_system="odoo",
            source_resource="crm.lead",
        ),
    ]

    def test_legacy_crm_field_has_no_provenance(self):
        f = CRMField(name="phone")
        assert f.source_system is None
        assert f.source_resource is None
        d = scan_result_to_dict(scan_crm_fields([f], PLASTICS_DOMAIN_SPEC))
        assert d["matched"][0]["source_system"] is None
        assert d["matched"][0]["source_resource"] is None

    def test_scan_mapping_contains_source_resource(self):
        result = scan_crm_fields(self.ODOO_FIELDS, PLASTICS_DOMAIN_SPEC)
        assert result.matched_count == 2  # both phones match domain 'phone'
        resources = {m.source_resource for m in result.matched}
        assert resources == {"res.partner", "crm.lead"}
        assert all(m.source_system == "odoo" for m in result.matched)
        assert all(m.crm_field == "phone" for m in result.matched)  # name not rewritten

    def test_unmapped_field_contains_source_resource(self):
        d = scan_result_to_dict(scan_crm_fields(self.ODOO_FIELDS, PLASTICS_DOMAIN_SPEC))
        assert d["unmapped"] == [
            {
                "crm_field": "x_custom_material_notes",
                "source_system": "odoo",
                "source_resource": "crm.lead",
            }
        ]

    def test_coverage_counts_unique_domain_properties(self):
        # Both Odoo resources match domain 'phone'; both mappings survive but the
        # domain property is covered once, so coverage never exceeds 1.0.
        result = scan_crm_fields(self.ODOO_FIELDS, PLASTICS_DOMAIN_SPEC)
        assert result.matched_count == 2
        assert result.coverage_ratio == round(1 / 14, 4)  # scanner rounds to 4 dp
        full = [
            CRMField(name=p, field_type="string", source_system="odoo", source_resource=r)
            for r in ("res.partner", "crm.lead")
            for p in (
                "name",
                "city",
                "phone",
                "materials_handled",
                "contamination_tolerance_pct",
                "process_types",
                "min_mfi",
                "max_mfi",
                "certifications",
                "facility_size_sqft",
                "annual_capacity_lbs",
                "material_grade",
                "facility_tier",
                "buyer_class",
            )
        ]
        saturated = scan_crm_fields(full, PLASTICS_DOMAIN_SPEC)
        assert saturated.matched_count == 28
        assert saturated.coverage_ratio == 1.0
        assert saturated.missing_count == 0

    def test_missing_entries_carry_no_provenance(self):
        result = scan_crm_fields(self.ODOO_FIELDS, PLASTICS_DOMAIN_SPEC)
        assert all(m.source_system is None and m.source_resource is None for m in result.missing)
        d = scan_result_to_dict(result)
        assert set(d["missing"][0]) == {"domain_property", "impact_tier"}

    def test_scan_hash_distinguishes_source_resource(self):
        partner_only = [
            CRMField(
                name="phone", field_type="char", source_system="odoo", source_resource="res.partner"
            )
        ]
        lead_only = [
            CRMField(
                name="phone", field_type="char", source_system="odoo", source_resource="crm.lead"
            )
        ]
        h_partner = scan_crm_fields(partner_only, PLASTICS_DOMAIN_SPEC).scan_hash
        h_lead = scan_crm_fields(lead_only, PLASTICS_DOMAIN_SPEC).scan_hash
        h_legacy = scan_crm_fields(
            [CRMField(name="phone", field_type="char")], PLASTICS_DOMAIN_SPEC
        ).scan_hash
        assert len({h_partner, h_lead, h_legacy}) == 3
        assert (
            h_legacy
            == scan_crm_fields(
                [CRMField(name="phone", field_type="char")], PLASTICS_DOMAIN_SPEC
            ).scan_hash
        )
