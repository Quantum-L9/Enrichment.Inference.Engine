"""
Tests for Simulation Bridge — validates gate/scoring/inference/community/leverage/brief.

PREVIEW-01: the bridge is a demo/test fixture. The tests at the end pin that
`simulate` is not a runtime action and that every brief is labelled synthetic.
Run: pytest tests/test_simulation_bridge.py -v
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from constellation_node_sdk.runtime.execution import execute_transport_packet
from constellation_node_sdk.runtime.handlers import clear_handlers, register_handler
from constellation_node_sdk.transport import create_transport_packet
from constellation_node_sdk.transport.errors import TransportAuthorizationError

from app.services.simulation_bridge import (
    SYNTHETIC_OUTPUT_LABEL,
    GateVerdict,
    LeverageType,
    SimulationMode,
    analyze_leverage,
    brief_to_dict,
    generate_executive_brief,
    generate_synthetic_entities,
    run_gates,
    run_inference,
    run_scoring,
    simulate,
)

DOMAIN_SPEC = {
    "domain": {"id": "plastics-recycling", "version": "8.0.0"},
    "ontology": {
        "nodes": [
            {
                "label": "Partner",
                "properties": {
                    "name": {"type": "string"},
                    "city": {"type": "string"},
                    "phone": {"type": "string"},
                    "materials_handled": {"type": "list"},
                    "contamination_tolerance_pct": {"type": "float"},
                    "process_types": {"type": "list"},
                    "min_mfi": {"type": "float"},
                    "max_mfi": {"type": "float"},
                    "certifications": {"type": "list"},
                    "facility_size_sqft": {"type": "integer"},
                    "annual_capacity_lbs": {"type": "integer"},
                    "industries_served": {"type": "list"},
                    "equipment_types": {"type": "list"},
                    "material_forms_output": {"type": "list"},
                    "polymers_handled": {"type": "list"},
                },
            }
        ]
    },
    "gates": [
        {"candidate_property": "materials_handled"},
        {"candidate_property": "contamination_tolerance_pct", "type": "range", "max": 5.0},
        {"candidate_property": "process_types"},
        {"candidate_property": "min_mfi", "type": "range", "max": 10.0},
        {"candidate_property": "max_mfi", "type": "range", "min": 15.0},
    ],
    "scoring_dimensions": [
        {"candidate_property": "certifications", "weight": 2.0},
        {"candidate_property": "facility_size_sqft", "weight": 1.0, "max_value": 500000},
        {"candidate_property": "annual_capacity_lbs", "weight": 1.5, "max_value": 400000000},
    ],
}

CUSTOMER_CRM_FIELDS = ["name", "city", "phone"]


class TestSyntheticGeneration:
    def test_generates_correct_count(self):
        entities = generate_synthetic_entities(CUSTOMER_CRM_FIELDS, DOMAIN_SPEC, count=10)
        assert len(entities) == 10

    def test_only_includes_crm_fields(self):
        entities = generate_synthetic_entities(CUSTOMER_CRM_FIELDS, DOMAIN_SPEC, count=5)
        for e in entities:
            real_fields = {k for k in e if not k.startswith("_")}
            assert real_fields <= {"name", "city", "phone"}

    def test_deterministic_with_seed(self):
        e1 = generate_synthetic_entities(CUSTOMER_CRM_FIELDS, DOMAIN_SPEC, count=5, seed=99)
        e2 = generate_synthetic_entities(CUSTOMER_CRM_FIELDS, DOMAIN_SPEC, count=5, seed=99)
        for a, b in zip(e1, e2, strict=True):
            assert a["name"] == b["name"]


class TestGates:
    def test_insufficient_data_when_missing(self):
        results = run_gates({"name": "Test"}, {"materials_handled": ["HDPE"]}, DOMAIN_SPEC["gates"])
        insufficient = [r for r in results if r.verdict == GateVerdict.INSUFFICIENT_DATA]
        assert len(insufficient) >= 4  # most gates missing

    def test_overlap_gate_pass(self):
        entity = {"materials_handled": ["HDPE", "PP", "PET"]}
        query = {"materials_handled": ["HDPE", "LDPE"]}
        results = run_gates(entity, query, [{"candidate_property": "materials_handled"}])
        assert results[0].verdict == GateVerdict.PASS

    def test_range_gate_fail(self):
        entity = {"contamination_tolerance_pct": 12.0}
        results = run_gates(
            entity,
            {},
            [{"candidate_property": "contamination_tolerance_pct", "type": "range", "max": 5.0}],
        )
        assert results[0].verdict == GateVerdict.FAIL


class TestScoring:
    def test_scoring_returns_composite(self):
        entity = {
            "certifications": ["ISO 9001", "R2"],
            "facility_size_sqft": 100000,
            "annual_capacity_lbs": 200000000,
        }
        results, composite = run_scoring(entity, DOMAIN_SPEC["scoring_dimensions"])
        assert 0.0 < composite <= 1.0
        assert len(results) == 3

    def test_missing_field_scores_zero(self):
        entity = {"certifications": ["ISO 9001"]}
        results, _ = run_scoring(entity, DOMAIN_SPEC["scoring_dimensions"])
        missing = [r for r in results if r.raw_value is None]
        assert all(r.normalized_score == pytest.approx(0.0) for r in missing)


class TestInference:
    def test_material_grade_inference(self):
        fields = {
            "polymers_handled": ["HDPE"],
            "contamination_tolerance_pct": 1.5,
            "certifications": ["ISO 9001"],
        }
        inferred = run_inference(fields)
        assert "material_grade" in inferred
        assert inferred["material_grade"] == "prime"

    def test_missing_inputs_skips(self):
        fields = {"polymers_handled": ["HDPE"]}
        inferred = run_inference(fields)
        assert "material_grade" not in inferred  # missing contamination_tolerance_pct


class TestFullSimulation:
    def test_dual_simulation(self):
        seed_stats, enriched_stats, seed_ents, enriched_ents = simulate(
            CUSTOMER_CRM_FIELDS, DOMAIN_SPEC, entity_count=20
        )
        assert seed_stats.mode == SimulationMode.SEED_ONLY
        assert enriched_stats.mode == SimulationMode.FULL_GRAPH
        assert enriched_stats.gate_pass_rate >= seed_stats.gate_pass_rate
        assert enriched_stats.field_coverage >= seed_stats.field_coverage

    def test_enriched_has_communities(self):
        _, enriched_stats, _, _ = simulate(CUSTOMER_CRM_FIELDS, DOMAIN_SPEC, entity_count=20)
        assert enriched_stats.communities_found >= 1


class TestLeverage:
    def test_leverage_points_generated(self):
        seed_stats, enriched_stats, _, _ = simulate(
            CUSTOMER_CRM_FIELDS, DOMAIN_SPEC, entity_count=20
        )
        points = analyze_leverage(seed_stats, enriched_stats)
        assert len(points) >= 1
        types = {p.leverage_type for p in points}
        assert LeverageType.MATCHING_PRECISION in types or LeverageType.PIPELINE_VELOCITY in types


class TestExecutiveBrief:
    def test_brief_generation(self):
        seed_stats, enriched_stats, _, _ = simulate(
            CUSTOMER_CRM_FIELDS, DOMAIN_SPEC, entity_count=20
        )
        leverage = analyze_leverage(seed_stats, enriched_stats)
        brief = generate_executive_brief(
            "Acme Recycling", "plastics-recycling", seed_stats, enriched_stats, leverage
        )
        assert brief.customer_name == "Acme Recycling"
        assert brief.estimated_roi_multiple > 0
        assert "revops_impact" in brief_to_dict(brief)
        assert len(brief.revops_impact) == 5

    def test_brief_serialization(self):
        seed_stats, enriched_stats, _, _ = simulate(
            CUSTOMER_CRM_FIELDS, DOMAIN_SPEC, entity_count=10
        )
        leverage = analyze_leverage(seed_stats, enriched_stats)
        brief = generate_executive_brief(
            "Test Co", "plastics", seed_stats, enriched_stats, leverage
        )
        d = brief_to_dict(brief)
        assert "headline" in d
        assert "leverage_points" in d
        assert "combined_narrative" in d


# ── PREVIEW-01: demo fixture, not a runtime action ───────────────────────────


class TestSyntheticLabel:
    def test_brief_is_labelled_synthetic(self):
        seed_stats, enriched_stats, _, _ = simulate(
            CUSTOMER_CRM_FIELDS, DOMAIN_SPEC, entity_count=5, use_sonar=False
        )
        brief = generate_executive_brief(
            "Test Co",
            "plastics",
            seed_stats,
            enriched_stats,
            analyze_leverage(seed_stats, enriched_stats),
        )
        d = brief_to_dict(brief)
        assert d["synthetic"] is True
        assert d["provenance"] == SYNTHETIC_OUTPUT_LABEL
        assert d["provenance"]["runtime_action"] is False
        assert d["provenance"]["source"] == "app.services.simulation_bridge"
        assert "Synthetic simulation" in d["headline"]
        assert d["combined_narrative"].startswith("SYNTHETIC SIMULATION.")

    def test_label_is_a_copy_not_the_module_constant(self):
        seed_stats, enriched_stats, _, _ = simulate(
            CUSTOMER_CRM_FIELDS, DOMAIN_SPEC, entity_count=5, use_sonar=False
        )
        brief = generate_executive_brief("Test Co", "plastics", seed_stats, enriched_stats, [])
        brief_to_dict(brief)["provenance"]["kind"] = "tampered"
        assert SYNTHETIC_OUTPUT_LABEL["kind"] == "simulation"


class TestSimulateIsNotARuntimeAction:
    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        clear_handlers()
        yield
        clear_handlers()

    def test_simulate_absent_from_runtime_allowed_actions(self):
        from app.main import RUNTIME_ALLOWED_ACTIONS, _build_runtime_config

        assert "simulate" not in RUNTIME_ALLOWED_ACTIONS
        assert "simulate" not in _build_runtime_config().allowed_actions

    def test_simulate_absent_from_handler_map(self):
        from app.engines.handlers import get_handler_map

        assert "simulate" not in get_handler_map()

    @pytest.mark.asyncio
    async def test_sdk_runtime_refuses_simulate_even_with_a_registered_handler(self):
        """Allowed-actions is checked before dispatch: a stale registration cannot route it."""
        from app.main import NODE_NAME, RUNTIME_ALLOWED_ACTIONS

        handler = AsyncMock(return_value={"status": "completed"})
        register_handler("simulate", handler)
        packet = create_transport_packet(
            action="simulate",
            payload={"crm_field_names": ["name"], "customer_name": "Acme"},
            tenant="acme",
            source_node="gate",
            destination_node=NODE_NAME,
            reply_to="gate",
        )
        with pytest.raises(TransportAuthorizationError, match="action not allowed: simulate"):
            await execute_transport_packet(
                packet,
                node_name=NODE_NAME,
                dev_mode=True,
                allowed_actions=RUNTIME_ALLOWED_ACTIONS,
            )
        handler.assert_not_awaited()


class TestDemoDriver:
    @pytest.mark.asyncio
    async def test_customer_name_does_not_default_to_tenant(self):
        """EIE-SEC-01: the tenant id never becomes a customer-facing label by default."""
        from app.engines.handlers import handle_simulate

        with pytest.raises(ValueError, match="customer_name is required"):
            await handle_simulate("acme", {"crm_field_names": ["name"], "use_sonar": False})

    @pytest.mark.asyncio
    async def test_demo_driver_output_is_labelled_synthetic(self):
        from app.engines.handlers import handle_simulate

        result = await handle_simulate(
            "acme",
            {
                "crm_field_names": CUSTOMER_CRM_FIELDS,
                "customer_name": "Acme Recycling",
                "domain_id": "does-not-exist",
                "entity_count": 5,
                "use_sonar": False,
            },
        )
        assert result["synthetic"] is True
        assert result["customer"] == "Acme Recycling"
        assert result["customer"] != "acme"
