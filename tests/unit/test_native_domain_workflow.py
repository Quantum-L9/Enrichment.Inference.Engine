"""Native operator workflow regressions; no service or production calls."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from app.engines.convergence import schema_proposer as proposer
from app.engines.domain_yaml_reader import DomainPublicationError
from app.services import domain_spec_workflow as workflow
from scripts import domain_spec_operator as operator


def _proposal() -> proposer.SchemaProposalSet:
    return proposer.SchemaProposalSet(
        proposed_fields=[proposer.FieldProposal(field_name="capacity", field_type="float")],
        proposed_gates=[proposer.GateProposal(field_name="capacity")],
        proposed_scoring_dimensions=[proposer.ScoringDimensionProposal(field_name="capacity")],
        version_bump="1.1.0-discovered",
    )


def _bound_proposal(source_bytes: bytes, node_label: str = "Buyer") -> proposer.SchemaProposalSet:
    tenant_id = yaml.safe_load(source_bytes)["name"]
    digest = hashlib.sha256(source_bytes).hexdigest()
    return proposer.SchemaProposalSet(
        domain=tenant_id,
        tenant_id=tenant_id,
        discovery_spec_sha256=digest,
        ownership_contract_version=1,
        proposed_fields=[
            proposer.FieldProposal(
                field_name="capacity",
                field_type="float",
                node_label=node_label,
                discovery_spec_sha256=digest,
                ownership_contract_version=1,
            )
        ],
        proposed_gates=[proposer.GateProposal(field_name="capacity")],
        proposed_scoring_dimensions=[proposer.ScoringDimensionProposal(field_name="capacity")],
        version_bump="1.1.0-discovered",
    )


def _source_bytes(base: dict) -> bytes:
    return yaml.safe_dump(base, sort_keys=False, allow_unicode=True).encode("utf-8")


def _base() -> dict:
    return {
        "name": "alpha",
        "version": "1.0.0",
        "ontology": {
            "nodes": [
                {
                    "label": "Facility",
                    "properties": [{"name": "id", "type": "string"}],
                    "enrichment_hints": {"objective_template": "Preserve me"},
                },
                {"label": "Buyer", "properties": [{"name": "id", "type": "string"}]},
            ]
        },
        "inference_rules": [{"rule_id": "reviewed-rule"}],
        "semantic_registry": {"enabled": False},
    }


def test_adr010_proposes_only_enrichment_fields() -> None:
    batch = [
        {
            "final_fields": {"capacity": float(i), "category": "A" if i % 2 else "B"},
            "final_field_confidences": {"capacity": 0.9, "category": 0.9},
        }
        for i in range(8)
    ]
    result = proposer.propose(batch, {"version": "1.0.0"})
    assert {p.field_name for p in result.proposed_fields} == {"capacity", "category"}
    assert result.proposed_gates == []
    assert result.proposed_scoring_dimensions == []
    assert "Proposed Gates" not in result.yaml_diff
    assert "Proposed Scoring" not in result.yaml_diff


def test_apply_requires_explicit_multi_node_target() -> None:
    with pytest.raises(ValueError, match="node_label"):
        proposer.apply(_base(), [proposer.ApprovalDecision(field_name="capacity")], _proposal())
    # An operator-supplied label cannot supply a missing historical owner.
    base = _base()
    before = copy.deepcopy(base)
    legacy = _proposal()
    with pytest.raises(ValueError, match="node_label"):
        proposer.apply(
            base,
            [proposer.ApprovalDecision(field_name="capacity", node_label="Buyer")],
            legacy,
            node_label="Buyer",
            source_sha256=hashlib.sha256(_source_bytes(base)).hexdigest(),
        )
    assert base == before
    assert legacy.proposed_fields[0].node_label is None


def test_apply_recorded_owner_needs_no_operator_target() -> None:
    source_bytes = _source_bytes(_base())
    base = yaml.safe_load(source_bytes)
    before = copy.deepcopy(base)
    result = proposer.apply(
        base,
        [proposer.ApprovalDecision(field_name="capacity", node_label="Buyer")],
        _bound_proposal(source_bytes),
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
    )
    assert result["ontology"]["nodes"][1]["properties"][-1]["name"] == "capacity"
    assert result["ontology"]["nodes"][0] == before["ontology"]["nodes"][0]
    assert result["version"] == "1.1.0-discovered"
    assert base == before


def test_apply_operator_label_is_only_a_recorded_owner_assertion() -> None:
    source_bytes = _source_bytes(_base())
    base = yaml.safe_load(source_bytes)
    before = copy.deepcopy(base)
    with pytest.raises(ValueError, match="assertion conflicts with recorded proposal owner"):
        proposer.apply(
            base,
            [proposer.ApprovalDecision(field_name="capacity", node_label="Buyer")],
            _bound_proposal(source_bytes),
            node_label="Facility",
            source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        )
    assert base == before


def test_apply_targets_node_preserves_other_native_sections() -> None:
    base = _base()
    before = copy.deepcopy(base)
    source_bytes = _source_bytes(base)
    result = proposer.apply(
        base,
        [proposer.ApprovalDecision(field_name="capacity", node_label="Buyer")],
        _bound_proposal(source_bytes),
        node_label="Buyer",
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
    )
    assert base == before
    assert result["ontology"]["nodes"][0] == before["ontology"]["nodes"][0]
    assert result["ontology"]["nodes"][1]["properties"][-1]["name"] == "capacity"
    assert result["inference_rules"] == before["inference_rules"]
    assert result["semantic_registry"] == before["semantic_registry"]
    assert "gates" not in result
    assert "scoring" not in result


def test_apply_does_not_overwrite_existing_property_by_default() -> None:
    base = _base()
    base["ontology"]["nodes"][1]["properties"].append(
        {"name": "capacity", "type": "integer", "description": "Human meaning"}
    )
    source_bytes = _source_bytes(base)
    with pytest.raises(ValueError, match="already exists"):
        proposer.apply(
            base,
            [proposer.ApprovalDecision(field_name="capacity", node_label="Buyer")],
            _bound_proposal(source_bytes),
            node_label="Buyer",
            source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        )
    result = proposer.apply(
        base,
        [proposer.ApprovalDecision(field_name="capacity", node_label="Buyer")],
        _bound_proposal(source_bytes),
        node_label="Buyer",
        allow_overwrite=True,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
    )
    prop = result["ontology"]["nodes"][1]["properties"][-1]
    assert prop["type"] == "float"
    assert prop["description"] == "Human meaning"


def test_apply_invalid_target_does_not_bump() -> None:
    source_bytes = _source_bytes(_base())
    base = yaml.safe_load(source_bytes)
    before = copy.deepcopy(base)
    with pytest.raises(ValueError, match="node_label"):
        proposer.apply(
            base,
            [proposer.ApprovalDecision(field_name="capacity", node_label="Buyer")],
            _bound_proposal(source_bytes),
            node_label="Unknown",
            source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        )
    assert base == before


def test_refused_fields_do_not_change_version() -> None:
    base = _base()
    source_bytes = _source_bytes(base)
    assert (
        proposer.apply(
            base,
            [proposer.ApprovalDecision(field_name="capacity", node_label="Buyer", approved=False)],
            _bound_proposal(source_bytes),
            node_label="Buyer",
            source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        )
        == base
    )


def test_legacy_audit_reconstruction_never_recreates_ranking_proposals() -> None:
    result = proposer.revision_from_approved(
        {"version": "1.0.0"},
        fields=_proposal().proposed_fields,
        gates=_proposal().proposed_gates,
        dims=_proposal().proposed_scoring_dimensions,
    )
    assert result.proposed_gates == []
    assert result.proposed_scoring_dimensions == []


def _native_template() -> bytes:
    return (Path(__file__).parents[2] / "domains/plasticos/spec.yaml").read_bytes()


def _audit_record(name: str = "dock_count", **changes) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "tenant_id": "plasticos",
            "domain": "plasticos",
            "batch_run_id": "fixture-audit-batch",
            "approval_status": "approved",
            "field_name": name,
            "field_type": "integer",
            "source": "enrichment",
            "fill_rate": 0.9,
            "avg_confidence": 0.8,
            "reviewed_by": "Fixture Reviewer",
            "reviewed_at": datetime.now(UTC),
            "node_label": "Facility",
            "discovery_spec_sha256": hashlib.sha256(_native_template()).hexdigest(),
            "ownership_contract_version": 1,
            **changes,
        }
    )


def test_scan_reuses_existing_scanner_seed_and_redacts_values(monkeypatch) -> None:
    scan = workflow.crm_field_scanner.scan_crm_fields
    generate = workflow.crm_field_scanner.generate_seed_yaml
    calls = []

    def tracked_scan(*args, **kwargs):
        calls.append("scan")
        return scan(*args, **kwargs)

    def tracked_seed(*args, **kwargs):
        calls.append("seed")
        return generate(*args, **kwargs)

    monkeypatch.setattr(workflow.crm_field_scanner, "scan_crm_fields", tracked_scan)
    monkeypatch.setattr(workflow.crm_field_scanner, "generate_seed_yaml", tracked_seed)
    template = _native_template()
    seed, report = workflow.scan_to_seed(
        {
            "fields": [
                {
                    "name": "city",
                    "sample_values": ["sensitive-fixture-data"],
                    "description": "sensitive-fixture-data",
                },
                {"name": "unknown_crm_field"},
            ]
        },
        template,
        tenant_id="alpha",
        source_identifier="fixture-crm/res.partner",
    )
    assert calls == ["scan", "seed"]
    assert seed["domain"]["id"] == "alpha"
    assert seed["domain"]["version"].endswith("-seed")
    assert seed["metadata"]["template"]["id"] == "plasticos"
    assert seed["metadata"]["draft_only"] is True
    assert report["tenant_id"] == "alpha"
    assert report["source_identifier"] == "fixture-crm/res.partner"
    assert report["template_sha256"] == hashlib.sha256(template).hexdigest()
    assert report["matched"][0]["domain_property"] == "city"
    assert report["unmapped"][0]["crm_field"] == "unknown_crm_field"
    assert report["missing"]
    assert "sensitive-fixture-data" not in json.dumps([seed, report])


def test_scan_ambiguity_is_reported_not_silently_selected() -> None:
    seed, report = workflow.scan_to_seed(
        [{"name": "city"}, {"name": "custom_city"}, {"name": "state"}],
        _native_template(),
        tenant_id="alpha",
        source_identifier="fixture-crm",
    )
    assert report["ambiguous"][0]["normalized_field"] == "city"
    assert [item["domain_property"] for item in report["matched"]] == ["state"]
    assert [item["name"] for item in seed["ontology"]["nodes"][0]["properties"]] == ["state"]


def test_native_shaped_seed_and_external_rule_ref_are_not_publishable() -> None:
    raw = yaml.safe_load(_native_template())
    raw["version"] = "0.1.0-seed"
    with pytest.raises(DomainPublicationError, match="Seed/draft"):
        workflow.materialize_approved_spec(
            raw,
            proposer.SchemaProposalSet(),
            [],
            tenant_id="plasticos",
            node_label="Facility",
        )
    raw["version"] = "1.0.0"
    raw.pop("inference_rules")
    raw["inference_rules_path"] = "rules/reviewed.yaml"
    with pytest.raises(DomainPublicationError, match="inline"):
        workflow._native_bytes(yaml.safe_dump(raw).encode(), "plasticos")


async def test_audit_materialization_requires_explicit_target_and_never_publishes(
    monkeypatch,
) -> None:
    approved = _audit_record()
    rejected = _audit_record("refused_field", approval_status="rejected")
    audit = AsyncMock(return_value=[approved, rejected])
    monkeypatch.setattr(workflow.pg_store, "get_approved_schema_proposals", audit)
    discovery_bytes = _native_template()
    discovery_specs = {hashlib.sha256(discovery_bytes).hexdigest(): discovery_bytes}
    raw = yaml.safe_load(discovery_bytes)
    original = copy.deepcopy(raw)
    candidate, batches = await workflow.materialize_audited_spec(
        raw,
        tenant_id="plasticos",
        domain="plasticos",
        node_label="Facility",
        discovery_specs=discovery_specs,
    )
    audit.assert_awaited_once_with(tenant_id="plasticos", domain="plasticos")
    updated = yaml.safe_load(candidate)
    assert batches == ["fixture-audit-batch"]
    assert "dock_count" in {p["name"] for p in updated["ontology"]["nodes"][0]["properties"]}
    assert "refused_field" not in {p["name"] for p in updated["ontology"]["nodes"][0]["properties"]}
    assert updated["inference_rules"] == original["inference_rules"]
    assert updated["semantic_registry"] == original["semantic_registry"]
    assert raw == original
    # Re-materializing approved historical rows onto completed bytes is a no-op.
    second, second_batches = await workflow.materialize_audited_spec(
        updated,
        tenant_id="plasticos",
        domain="plasticos",
        node_label="Facility",
        discovery_specs=discovery_specs,
    )
    assert second == candidate
    assert second_batches == []
    assert discovery_specs == {approved.discovery_spec_sha256: discovery_bytes}


async def test_audit_materialization_rejects_cross_tenant_and_unreviewed_records(
    monkeypatch,
) -> None:
    discovery_bytes = _native_template()
    discovery_specs = {hashlib.sha256(discovery_bytes).hexdigest(): discovery_bytes}
    raw = yaml.safe_load(discovery_bytes)
    audit = AsyncMock()
    monkeypatch.setattr(workflow.pg_store, "get_approved_schema_proposals", audit)
    with pytest.raises(DomainPublicationError, match="domain must equal"):
        await workflow.materialize_audited_spec(
            raw,
            tenant_id="plasticos",
            domain="alpha",
            node_label="Facility",
            discovery_specs=discovery_specs,
        )
    audit.assert_not_awaited()
    for record in [
        _audit_record(tenant_id="alpha"),
        _audit_record(reviewed_by=""),
        _audit_record(reviewed_at=None),
    ]:
        audit.return_value = [record]
        with pytest.raises(DomainPublicationError):
            await workflow.materialize_audited_spec(
                raw,
                tenant_id="plasticos",
                domain="plasticos",
                node_label="Facility",
                discovery_specs=discovery_specs,
            )


async def test_materialize_batch_filter_and_existing_field_conflict(monkeypatch) -> None:
    discovery_bytes = _native_template()
    discovery_specs = {hashlib.sha256(discovery_bytes).hexdigest(): discovery_bytes}
    raw = yaml.safe_load(discovery_bytes)
    audit = AsyncMock(return_value=[_audit_record("city", field_type="integer")])
    monkeypatch.setattr(workflow.pg_store, "get_approved_schema_proposals", audit)
    with pytest.raises(DomainPublicationError, match="different type"):
        await workflow.materialize_audited_spec(
            raw,
            tenant_id="plasticos",
            domain="plasticos",
            node_label="Facility",
            discovery_specs=discovery_specs,
        )
    with pytest.raises(DomainPublicationError, match="no approved"):
        await workflow.materialize_audited_spec(
            raw,
            tenant_id="plasticos",
            domain="plasticos",
            node_label="Facility",
            batch_run_ids=["missing-batch"],
            discovery_specs=discovery_specs,
        )


def test_cli_scan_to_seed_creates_only_draft_artifacts(tmp_path: Path, capsys) -> None:
    metadata = tmp_path / "crm.json"
    metadata.write_text(
        json.dumps({"fields": [{"name": "city", "sample_values": ["sensitive-fixture-data"]}]})
    )
    template = Path(__file__).parents[2] / "domains/plasticos/spec.yaml"
    code = operator.main(
        [
            "scan-to-seed",
            "--crm-metadata",
            str(metadata),
            "--template",
            str(template),
            "--tenant",
            "alpha",
            "--source",
            "fixture-crm",
            "--seed-output",
            str(tmp_path / "seed.yaml"),
            "--report-output",
            str(tmp_path / "scan.json"),
        ]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "sensitive-fixture-data" not in output
    assert yaml.safe_load((tmp_path / "seed.yaml").read_bytes())["metadata"]["draft_only"] is True
    assert json.loads((tmp_path / "scan.json").read_bytes())["draft_only"] is True
    assert not (tmp_path / "alpha/current.json").exists()


def test_cli_publish_requires_named_exact_review_and_is_separate(tmp_path: Path) -> None:
    template = tmp_path / "spec.yaml"
    template.write_bytes(_native_template())
    root = tmp_path / "store"
    digest = hashlib.sha256(template.read_bytes()).hexdigest()
    with pytest.raises(SystemExit):
        operator.build_parser().parse_args(
            ["publish", "--root", str(root), "--tenant", "plasticos", "--spec", str(template)]
        )
    assert (
        operator.main(
            [
                "publish",
                "--root",
                str(root),
                "--tenant",
                "plasticos",
                "--spec",
                str(template),
                "--reviewed-input-sha256",
                digest,
                "--reviewed-by",
                "Fixture Reviewer",
                "--approval-ref",
                "fixture-only",
            ]
        )
        == 0
    )
    assert json.loads((root / "plasticos/current.json").read_bytes())["sha256"] == digest


def test_cli_refuses_symlink_input_and_existing_output(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("[]")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        operator._read(str(link))
    with pytest.raises(FileExistsError):
        operator._write(str(target), b"must-not-overwrite")
    assert target.read_text() == "[]"


def test_conflicting_or_unproposed_decisions_are_refused() -> None:
    discovery_bytes = _native_template()
    raw = yaml.safe_load(discovery_bytes)
    for decisions in [
        [
            proposer.ApprovalDecision(field_name="capacity", node_label="Facility"),
            proposer.ApprovalDecision(field_name="capacity", node_label="Facility", approved=False),
        ],
        [proposer.ApprovalDecision(field_name="unknown", node_label="Facility")],
    ]:
        with pytest.raises(DomainPublicationError):
            workflow.materialize_approved_spec(
                raw,
                _bound_proposal(discovery_bytes, node_label="Facility"),
                decisions,
                tenant_id="plasticos",
                node_label="Facility",
                source_sha256=hashlib.sha256(discovery_bytes).hexdigest(),
            )
