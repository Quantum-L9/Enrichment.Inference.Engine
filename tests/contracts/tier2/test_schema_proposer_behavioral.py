"""Tier-2 behavioral contract tests for app.engines.convergence.schema_proposer.

L9 Master Kernel anchors:
- §3.1 — all Python and YAML fields MUST be snake_case
- §5.1 — TransportPacket / shared contract canonical-name drift list
- §5.2 — spec.yaml canonical-name drift list
- §2.3 INV-OBS-05 — structured logging required for any drop event

L9_META:
  tier: 2
  domain: convergence
  authority: L9 Master Kernel v3.0
  pr_class: app_code + tier2_test
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from app.engines.convergence.schema_proposer import (
    BANNED_FIELD_NAMES_L9_KERNEL,
    MIN_AVG_CONFIDENCE,
    MIN_FILL_RATE,
    ApprovalDecision,
    FieldProposal,
    SchemaProposalSet,
    apply,
    propose,
)

pytestmark = [pytest.mark.unit]


def _make_batch(
    field_name: str,
    value: Any,
    confidence: float,
    size: int = 10,
) -> list[dict[str, Any]]:
    """Build a synthetic batch where field_name appears in every result.

    Uses the flat confidence-fixture shape ({field: float}) to align with
    the existing tests/test_schema_proposer.py convention.
    """
    return [
        {
            "final_fields": {field_name: value},
            "final_field_confidences": {field_name: confidence},
        }
        for _ in range(size)
    ]


def _empty_yaml(domain: str = "test_domain") -> dict[str, Any]:
    return {
        "domain": domain,
        "version": "1.0.0",
        "ontology": {"nodes": {"Entity": {"properties": {}}}},
        "gates": [],
        "scoring": [],
    }


def _yaml_with_fields(domain: str, fields: list[str]) -> dict[str, Any]:
    return {
        "domain": domain,
        "version": "1.0.0",
        "ontology": {"nodes": {"Entity": {"properties": {f: {"type": "string"} for f in fields}}}},
        "gates": [],
        "scoring": [],
    }


class TestProposeSchemaForKnownDomain:
    """propose() returns a valid SchemaProposalSet for a well-formed batch."""

    def test_returns_schema_proposal_set(self) -> None:
        batch = _make_batch("industry_vertical", "SaaS", confidence=0.90, size=10)
        result = propose(batch, _empty_yaml("b2b_saas"), domain="b2b_saas")
        assert isinstance(result, SchemaProposalSet)

    def test_domain_is_set(self) -> None:
        batch = _make_batch("industry_vertical", "SaaS", confidence=0.90, size=10)
        result = propose(batch, _empty_yaml("b2b_saas"), domain="b2b_saas")
        assert result.domain == "b2b_saas"

    def test_proposed_fields_non_empty_above_thresholds(self) -> None:
        assert MIN_AVG_CONFIDENCE < 0.90
        assert MIN_FILL_RATE < 1.0
        batch = _make_batch("industry_vertical", "SaaS", confidence=0.90, size=10)
        result = propose(batch, _empty_yaml("b2b_saas"), domain="b2b_saas")
        assert len(result.proposed_fields) > 0

    def test_yaml_diff_non_empty(self) -> None:
        batch = _make_batch("industry_vertical", "SaaS", confidence=0.90, size=10)
        result = propose(batch, _empty_yaml("b2b_saas"), domain="b2b_saas")
        assert result.yaml_diff.strip() != ""

    def test_version_bump_matches_discovered_pattern(self) -> None:
        batch = _make_batch("industry_vertical", "SaaS", confidence=0.90, size=10)
        result = propose(batch, _empty_yaml("b2b_saas"), domain="b2b_saas")
        assert re.fullmatch(r"\d+\.\d+\.\d+-discovered", result.version_bump), (
            f"version_bump '{result.version_bump}' does not match x.y.z-discovered"
        )

    def test_entities_analysed_equals_batch_size(self) -> None:
        batch = _make_batch("industry_vertical", "SaaS", confidence=0.90, size=10)
        result = propose(batch, _empty_yaml("b2b_saas"), domain="b2b_saas")
        assert result.entities_analysed == 10

    def test_empty_batch_returns_empty_proposal_set(self) -> None:
        result = propose([], _empty_yaml(), domain="empty_domain")
        assert result.proposed_fields == []
        assert result.proposed_gates == []
        assert result.proposed_scoring_dimensions == []

    def test_below_confidence_threshold_yields_no_proposals(self) -> None:
        assert MIN_AVG_CONFIDENCE > 0.50
        batch = _make_batch("low_conf_field", "value", confidence=0.50, size=10)
        result = propose(batch, _empty_yaml(), domain="test")
        assert len(result.proposed_fields) == 0

    def test_below_fill_rate_threshold_yields_no_proposals(self) -> None:
        assert MIN_FILL_RATE > 0.50
        half_filled = [
            {
                "final_fields": {"sparse_field": "val" if i < 5 else None},
                "final_field_confidences": {"sparse_field": 0.90},
            }
            for i in range(10)
        ]
        result = propose(half_filled, _empty_yaml(), domain="test")
        field_names = [fp.field_name for fp in result.proposed_fields]
        assert "sparse_field" not in field_names


class TestProposeSchemaRespectsCurrentYamlScope:
    """propose() honors the current_yaml argument for existing-field exclusion.

    The real invariant is "the proposer respects the spec it was given,"
    not multi-tenant isolation (which is a Neo4j-query concern, INV-ARCH-06).
    """

    def test_existing_field_in_yaml_is_not_re_proposed(self) -> None:
        current_yaml = _yaml_with_fields("d_a", ["industry_vertical"])
        batch = _make_batch("industry_vertical", "SaaS", confidence=0.90, size=10)
        result = propose(batch, current_yaml, domain="d_a")
        proposed_names = [fp.field_name for fp in result.proposed_fields]
        assert "industry_vertical" not in proposed_names

    def test_distinct_yamls_suppress_independently(self) -> None:
        yaml_a = _yaml_with_fields("d_a", ["crm_owner_a"])
        yaml_b = _yaml_with_fields("d_b", ["crm_owner_b"])
        batch = [
            {
                "final_fields": {"crm_owner_a": "Alice", "crm_owner_b": "Bob"},
                "final_field_confidences": {"crm_owner_a": 0.88, "crm_owner_b": 0.88},
            }
            for _ in range(10)
        ]

        result_a = propose(batch, yaml_a, domain="d_a")
        result_b = propose(batch, yaml_b, domain="d_b")

        proposed_a = [fp.field_name for fp in result_a.proposed_fields]
        proposed_b = [fp.field_name for fp in result_b.proposed_fields]

        assert "crm_owner_a" not in proposed_a
        assert "crm_owner_b" in proposed_a
        assert "crm_owner_a" in proposed_b
        assert "crm_owner_b" not in proposed_b

    def test_novel_field_proposed_when_not_in_provided_yaml(self) -> None:
        current_yaml = _yaml_with_fields("d_a", ["old_field"])
        batch = _make_batch("brand_new_signal", "high", confidence=0.92, size=10)
        result = propose(batch, current_yaml, domain="d_a")
        proposed_names = [fp.field_name for fp in result.proposed_fields]
        assert "brand_new_signal" in proposed_names


class TestProposeSchemaEnforcesKernelFieldNameInvariants:
    """propose() and apply() reject names that violate L9 §3.1 / §5.1 / §5.2."""

    @pytest.mark.parametrize("banned_name", sorted(BANNED_FIELD_NAMES_L9_KERNEL))
    def test_banned_kernel_name_not_in_proposed_fields(self, banned_name: str) -> None:
        batch = _make_batch(banned_name, "value", confidence=0.95, size=10)
        result = propose(batch, _empty_yaml(), domain="test")
        proposed_names = [fp.field_name for fp in result.proposed_fields]
        assert banned_name not in proposed_names, (
            f"L9-kernel banned name '{banned_name}' must not appear in proposals"
        )

    @pytest.mark.parametrize(
        "non_canonical_name",
        [
            "fooBar",
            "myField",
            "PascalCase",
            "dotted.path",
            "_leading_underscore",
            "1leading_digit",
            "with-dash",
            "with space",
        ],
    )
    def test_non_snake_case_name_not_in_proposed_fields(self, non_canonical_name: str) -> None:
        batch = _make_batch(non_canonical_name, "value", confidence=0.95, size=10)
        result = propose(batch, _empty_yaml(), domain="test")
        proposed_names = [fp.field_name for fp in result.proposed_fields]
        assert non_canonical_name not in proposed_names, (
            f"Non-snake_case '{non_canonical_name}' must not appear (L9 §3.1)"
        )

    def test_safe_field_proposed_alongside_banned_in_same_batch(self) -> None:
        batch = [
            {
                "final_fields": {
                    "industry_vertical": "SaaS",
                    "traceId": "abc-123",
                },
                "final_field_confidences": {
                    "industry_vertical": 0.95,
                    "traceId": 0.95,
                },
            }
            for _ in range(10)
        ]
        result = propose(batch, _empty_yaml(), domain="test")
        proposed_names = [fp.field_name for fp in result.proposed_fields]
        assert "industry_vertical" in proposed_names
        assert "traceId" not in proposed_names

    def test_banned_field_names_l9_kernel_is_frozenset(self) -> None:
        assert isinstance(BANNED_FIELD_NAMES_L9_KERNEL, frozenset)

    def test_banned_field_names_l9_kernel_includes_required_kernel_entries(
        self,
    ) -> None:
        required_5_1 = {
            "packetid",
            "packetID",
            "traceId",
            "threadId",
            "parentIds",
            "sourceNode",
            "onBehalfOf",
            "orgId",
        }
        required_5_2 = {
            "matchentities",
            "nodelabels",
            "candidateprop",
            "null_semantics",
            "computation_type",
            "targetnode",
            "idproperty",
        }
        missing = (required_5_1 | required_5_2) - BANNED_FIELD_NAMES_L9_KERNEL
        assert not missing, f"Missing kernel-required entries: {sorted(missing)}"

    def test_apply_blocks_banned_name_via_defense_in_depth(self) -> None:
        """A hand-built proposal with a banned name must NOT land in YAML on apply()."""
        banned_proposal = SchemaProposalSet(
            domain="test",
            proposed_fields=[
                FieldProposal(
                    field_name="traceId",
                    field_type="string",
                    fill_rate=1.0,
                    avg_confidence=0.99,
                ),
                FieldProposal(
                    field_name="industry_vertical",
                    field_type="string",
                    fill_rate=1.0,
                    avg_confidence=0.99,
                ),
            ],
            yaml_diff="",
            version_bump="1.1.0-discovered",
            entities_analysed=10,
        )
        decisions = [
            ApprovalDecision(field_name="traceId", approved=True),
            ApprovalDecision(field_name="industry_vertical", approved=True),
        ]

        updated = apply(_empty_yaml(), decisions, banned_proposal)

        props = updated["ontology"]["nodes"]["Entity"]["properties"]
        assert "traceId" not in props, (
            "apply() must drop banned names even when approved (defense-in-depth)"
        )
        assert "industry_vertical" in props
