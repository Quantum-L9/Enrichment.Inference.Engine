"""Persisted discovery owners: proposal/application tests without external services.

L9_META:
  tier: 2
  authority: native-domain-completion-v3-g27r
"""

from __future__ import annotations

import copy
import hashlib
from typing import Any

import pytest
import yaml

from app.engines.convergence import schema_proposer as proposer

pytestmark = pytest.mark.unit


def _base() -> dict[str, Any]:
    return {
        "name": "owner-fixture",
        "version": "1.0.0",
        "ontology": {
            "nodes": [
                {"label": "Facility", "properties": [{"name": "id", "type": "string"}]},
                {"label": "Buyer", "properties": [{"name": "id", "type": "string"}]},
            ]
        },
        "inference_rules": [],
        "semantic_registry": {"enabled": False},
    }


def _digest(base: dict[str, Any]) -> str:
    return hashlib.sha256(yaml.safe_dump(base, sort_keys=False).encode()).hexdigest()


def _result(owner: str | None, value: Any, source: str = "inference") -> dict[str, Any]:
    return {
        "node_label": owner,
        "final_fields": {"shared_metric": value},
        "final_field_confidences": {"shared_metric": {"confidence": 0.9, "source": source}},
    }


def _proposal(base: dict[str, Any], batch: list[dict[str, Any]] | None = None):
    return proposer.propose(
        batch if batch is not None else [_result("Facility", 3.0), _result("Buyer", "high", "crm")],
        base,
        domain="owner-fixture",
        tenant_id="owner-fixture",
        discovery_spec_sha256=_digest(base),
    )


def _decisions(approved: bool = True):
    return [
        proposer.ApprovalDecision(node_label=owner, field_name="shared_metric", approved=approved)
        for owner in ("Facility", "Buyer")
    ]


def test_owner_scoped_existing_field_does_not_suppress_other_owner() -> None:
    base = _base()
    base["ontology"]["nodes"][0]["properties"].append({"name": "shared_metric", "type": "float"})
    result = _proposal(base)
    assert [(fp.node_label, fp.field_name) for fp in result.proposed_fields] == [
        ("Buyer", "shared_metric")
    ]
    assert result.tenant_id == result.domain == "owner-fixture"
    assert result.discovery_spec_sha256 == _digest(base)
    assert result.ownership_contract_version == 1


def test_same_name_statistics_sources_and_denominators_are_per_owner() -> None:
    base = _base()
    result = _proposal(
        base,
        [
            _result("Facility", 3.0),
            _result("Buyer", "high", "crm"),
            {"node_label": "Facility", "final_fields": {}},
            _result("Facility", 5.0),
        ],
    )
    fields = {fp.node_label: fp for fp in result.proposed_fields}
    assert fields["Facility"].fill_rate == 0.6667
    assert fields["Facility"].field_type == "float"
    assert fields["Facility"].source == "inference"
    assert fields["Buyer"].fill_rate == 1.0
    assert fields["Buyer"].field_type == "string"
    assert fields["Buyer"].source == "crm"
    assert all(
        fp.discovery_spec_sha256 == _digest(base) and fp.ownership_contract_version == 1
        for fp in fields.values()
    )
    assert "Facility.shared_metric" in result.yaml_diff
    assert "Buyer.shared_metric" in result.yaml_diff


def test_source_attribution_refusal_is_still_per_owner() -> None:
    base = _base()
    with pytest.raises(proposer.SourceAttributionError) as error:
        _proposal(
            base,
            [
                _result("Facility", 1, "crm"),
                _result("Facility", 2, "manual"),
                _result("Buyer", "A", "inference"),
            ],
        )
    assert error.value.field_sources == (("Facility.shared_metric", ("crm", "manual")),)


@pytest.mark.parametrize("owner", [None, "Unknown", "Bad Label", "A" * 129])
def test_discovery_refuses_missing_or_invalid_owner_without_inference(owner) -> None:
    base = _base()
    with pytest.raises(ValueError, match="node_label"):
        _proposal(base, [_result(owner, 1)])


def test_explicit_single_owner_discovery_and_conflicting_result_refusal() -> None:
    base = _base()
    result = proposer.propose(
        [_result(None, 1)],
        base,
        "owner-fixture",
        node_label="Facility",
        tenant_id="owner-fixture",
        discovery_spec_sha256=_digest(base),
    )
    assert result.proposed_fields[0].node_label == "Facility"
    with pytest.raises(ValueError, match="node_label"):
        proposer.propose(
            [_result("Buyer", 1)],
            base,
            "owner-fixture",
            node_label="Facility",
            tenant_id="owner-fixture",
            discovery_spec_sha256=_digest(base),
        )


@pytest.mark.parametrize("digest", [None, "F" * 64, "abc", "g" * 64])
def test_bound_discovery_requires_lowercase_digest(digest) -> None:
    with pytest.raises(ValueError, match="digest"):
        proposer.propose(
            [],
            _base(),
            "owner-fixture",
            node_label="Facility",
            tenant_id="owner-fixture",
            discovery_spec_sha256=digest,
        )


def test_discovery_refuses_cross_tenant_domain() -> None:
    with pytest.raises(ValueError, match="tenant"):
        proposer.propose(
            [],
            _base(),
            "other",
            node_label="Facility",
            tenant_id="owner-fixture",
            discovery_spec_sha256=_digest(_base()),
        )


@pytest.mark.parametrize("accepted", ["Facility", "Buyer"])
def test_pair_decisions_apply_only_recorded_owner_with_one_version_bump(accepted) -> None:
    base = _base()
    original = copy.deepcopy(base)
    proposal = _proposal(base)
    decisions = [d.model_copy(update={"approved": d.node_label == accepted}) for d in _decisions()]
    keys = proposer.applied_field_keys(decisions, proposal)
    assert keys == [(accepted, "shared_metric")]
    updated = proposer.apply(base, decisions, proposal, source_digest=_digest(base))
    assert updated["version"] == "1.1.0-discovered"
    for node in updated["ontology"]["nodes"]:
        assert ("shared_metric" in {p["name"] for p in node["properties"]}) == (
            node["label"] == accepted
        )
    assert base == original
    assert updated["inference_rules"] == original["inference_rules"]
    assert updated["semantic_registry"] == original["semantic_registry"]


def test_multi_owner_applies_atomically_and_name_helper_does_not_collapse() -> None:
    base = _base()
    proposal = _proposal(base)
    with pytest.raises(ValueError, match="owner"):
        proposer.applied_field_names(_decisions(), proposal)
    updated = proposer.apply(base, _decisions(), proposal, source_digest=_digest(base))
    assert updated["version"] == "1.1.0-discovered"
    assert all(
        node["properties"][-1]["name"] == "shared_metric" for node in updated["ontology"]["nodes"]
    )
    with pytest.raises(ValueError, match="node_label"):
        proposer.apply(
            base, _decisions(), proposal, node_label="Buyer", source_digest=_digest(base)
        )
    assert base == _base()


@pytest.mark.parametrize(
    "kind", ["duplicate-proposal", "duplicate-decision", "unknown-pair", "unbound-decision"]
)
def test_pair_membership_and_duplicates_fail_before_apply(kind) -> None:
    base = _base()
    proposal = _proposal(base)
    decisions = _decisions()
    if kind == "duplicate-proposal":
        proposal.proposed_fields.append(proposal.proposed_fields[0])
    elif kind == "duplicate-decision":
        decisions.append(decisions[0])
    elif kind == "unknown-pair":
        decisions.append(
            proposer.ApprovalDecision(node_label="Unknown", field_name="shared_metric")
        )
    else:
        decisions[0] = proposer.ApprovalDecision(field_name="shared_metric")
    with pytest.raises(ValueError):
        proposer.apply(base, decisions, proposal, source_digest=_digest(base))
    assert base == _base()


@pytest.mark.parametrize("approved", [False, True])
def test_native_legacy_rows_are_not_stamped_by_operator_label(approved) -> None:
    base = _base()
    proposal = proposer.SchemaProposalSet(
        proposed_fields=[proposer.FieldProposal(field_name="legacy")]
    )
    with pytest.raises(ValueError, match="bound"):
        proposer.apply(
            base,
            [proposer.ApprovalDecision(field_name="legacy", approved=approved)],
            proposal,
            node_label="Buyer",
            source_digest=_digest(base),
        )
    assert proposal.proposed_fields[0].node_label is None
    assert proposal.discovery_spec_sha256 is None


def test_noop_still_validates_owner_and_discovery_digest() -> None:
    base = _base()
    proposal = _proposal(base)
    assert proposer.apply(base, _decisions(False), proposal, source_digest=_digest(base)) == base
    for digest in (None, "f" * 64):
        with pytest.raises(ValueError, match="digest"):
            proposer.apply(base, _decisions(False), proposal, source_digest=digest)
    base["ontology"]["nodes"][0]["label"] = "Renamed"
    with pytest.raises(ValueError, match="node_label"):
        proposer.apply(
            base, _decisions(False), proposal, source_digest=proposal.discovery_spec_sha256
        )


def test_overwrite_preserves_metadata_on_recorded_owner_only() -> None:
    base = _base()
    proposal = _proposal(base)
    base["ontology"]["nodes"][0]["properties"].append(
        {"name": "shared_metric", "type": "integer", "description": "Human meaning"}
    )
    decisions = _decisions()
    with pytest.raises(ValueError, match="already exists"):
        proposer.apply(base, decisions, proposal, source_digest=proposal.discovery_spec_sha256)
    updated = proposer.apply(
        base,
        decisions,
        proposal,
        source_digest=proposal.discovery_spec_sha256,
        allow_overwrite=True,
    )
    assert updated["ontology"]["nodes"][0]["properties"][-1]["description"] == "Human meaning"
    assert updated["ontology"]["nodes"][1]["properties"][-1]["type"] == "string"


def test_live_source_observations_are_complete_not_defaulted_or_selected() -> None:
    base = _base()
    batch = [_result("Facility", 1)]
    batch[0]["final_field_confidences"] = {"shared_metric": {"confidence": 0.9}}
    batch[0]["field_source_observations"] = {"shared_metric": ["graph_inference"]}
    result = _proposal(base, batch)
    assert result.proposed_fields[0].source == "graph_inference"
    batch[0]["field_source_observations"]["shared_metric"] = ["inference", "enrichment"]
    with pytest.raises(proposer.SourceAttributionError) as error:
        _proposal(base, batch)
    assert error.value.field_sources == (("Facility.shared_metric", ("enrichment", "inference")),)


@pytest.mark.parametrize("observations", ["inference", [None], [1], [""], ["bad source "]])
def test_explicit_source_observations_must_be_lists_of_nonblank_strings(observations) -> None:
    result = _result("Facility", 1)
    result["field_source_observations"] = {"shared_metric": observations}
    with pytest.raises(ValueError, match="source observations"):
        _proposal(_base(), [result])


@pytest.mark.parametrize("observations", [{}, {"shared_metric": []}])
def test_unobserved_live_sources_do_not_fabricate_proposals(observations) -> None:
    result = _result("Facility", 1)
    result["field_source_observations"] = observations
    assert _proposal(_base(), [result]).proposed_fields == []
