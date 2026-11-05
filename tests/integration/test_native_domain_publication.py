"""Isolated local publication tests; review identities below are test fixtures only."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from app.engines.convergence import schema_proposer as proposer
from app.engines.domain_yaml_reader import DomainPublicationError, DomainYamlReader
from app.services import domain_spec_workflow as workflow

REPO = Path(__file__).parents[2]


def _spec(version: str = "1.0.0") -> bytes:
    raw = yaml.safe_load((REPO / "domains/plasticos/spec.yaml").read_bytes())
    raw["name"] = "alpha"
    raw["version"] = version
    return yaml.safe_dump(raw, sort_keys=False, allow_unicode=True).encode()


def _publish(root: Path, value: bytes, **kwargs) -> dict:
    return workflow.publish_effective_spec(
        root,
        "alpha",
        value,
        reviewed_input_sha256=hashlib.sha256(value).hexdigest(),
        reviewed_by="Fixture Reviewer",
        approval_ref="test-fixture-only/approval-1",
        **kwargs,
    )


def _current(root: Path) -> str:
    return json.loads((root / "alpha/current.json").read_text())["sha256"]


def test_native_publish_reader_restart_and_original_receipt(tmp_path: Path) -> None:
    first = _spec()
    receipt = _publish(tmp_path, first)
    sha256 = hashlib.sha256(first).hexdigest()
    assert _current(tmp_path) == sha256
    assert receipt["reviewed_input_sha256"] == sha256
    assert receipt["previous_sha256"] is None
    assert (tmp_path / f"alpha/revisions/{sha256}/spec.yaml").read_bytes() == first
    config = DomainYamlReader(tmp_path, require_published=True).load("alpha")
    assert config.version == "1.0.0"
    assert config.source_sha256 == sha256
    assert config.approval_ref == "test-fixture-only/approval-1"
    assert config.raw_spec["inference_rules"]
    assert _publish(tmp_path, first) == receipt


def test_cas_history_and_rollback_preserve_approval(tmp_path: Path) -> None:
    first, second = _spec(), _spec("1.1.0")
    receipt1 = _publish(tmp_path, first)
    receipt2 = _publish(tmp_path, second, expected_previous_sha256=receipt1["sha256"])
    assert receipt2["previous_sha256"] == receipt1["sha256"]
    before_receipt = (tmp_path / f"alpha/revisions/{receipt1['sha256']}/receipt.json").read_bytes()
    restored = workflow.rollback_effective_spec(
        tmp_path,
        "alpha",
        receipt1["sha256"],
        expected_previous_sha256=receipt2["sha256"],
    )
    assert restored == receipt1
    assert _current(tmp_path) == receipt1["sha256"]
    assert (
        tmp_path / f"alpha/revisions/{receipt1['sha256']}/receipt.json"
    ).read_bytes() == before_receipt
    assert len(list((tmp_path / "alpha/revisions").iterdir())) == 2


def test_stale_cas_never_changes_current(tmp_path: Path) -> None:
    receipt = _publish(tmp_path, _spec())
    with pytest.raises(DomainPublicationError, match="Compare-and-swap"):
        _publish(tmp_path, _spec("1.1.0"))
    assert _current(tmp_path) == receipt["sha256"]
    with pytest.raises(DomainPublicationError, match="Compare-and-swap"):
        workflow.rollback_effective_spec(
            tmp_path,
            "alpha",
            receipt["sha256"],
            expected_previous_sha256="f" * 64,
        )


def test_competing_publishers_have_exactly_one_winner(tmp_path: Path) -> None:
    receipt = _publish(tmp_path, _spec())

    def contender(version: str) -> bool:
        try:
            _publish(tmp_path, _spec(version), expected_previous_sha256=receipt["sha256"])
        except DomainPublicationError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contender, ("1.1.0", "1.2.0")))
    assert sorted(results) == [False, True]
    assert len(list((tmp_path / "alpha/revisions").iterdir())) == 2


@pytest.mark.parametrize(
    "bad_value",
    [
        b"domain: {id: alpha, version: 0.1.0-seed}\nontology: {nodes: []}\n",
        b"name: alpha\nversion: 1.0.0\n",
        b"name: [invalid\n",
    ],
)
def test_seed_or_invalid_native_bytes_never_activate(tmp_path: Path, bad_value: bytes) -> None:
    with pytest.raises(ValueError):
        _publish(tmp_path, bad_value)
    assert not (tmp_path / "alpha/current.json").exists()


@pytest.mark.parametrize("bad_tenant", ["../alpha", "alpha/escape", "system-one", "Alpha"])
def test_path_escape_tenant_rejected(tmp_path: Path, bad_tenant: str) -> None:
    value = _spec()
    with pytest.raises(DomainPublicationError):
        workflow.publish_effective_spec(
            tmp_path,
            bad_tenant,
            value,
            reviewed_input_sha256=hashlib.sha256(value).hexdigest(),
            reviewed_by="Fixture Reviewer",
            approval_ref="fixture-only",
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bad_review", ["", " ", None])
def test_explicit_named_review_required(tmp_path: Path, bad_review: str) -> None:
    value = _spec()
    with pytest.raises(DomainPublicationError):
        workflow.publish_effective_spec(
            tmp_path,
            "alpha",
            value,
            reviewed_input_sha256=hashlib.sha256(value).hexdigest(),
            reviewed_by=bad_review,
            approval_ref="fixture-only",
        )
    assert list(tmp_path.iterdir()) == []


def test_exact_digest_and_approval_ref_required(tmp_path: Path) -> None:
    value = _spec()
    for digest, reference in [("f" * 64, "fixture-only"), (hashlib.sha256(value).hexdigest(), "")]:
        with pytest.raises(DomainPublicationError):
            workflow.publish_effective_spec(
                tmp_path,
                "alpha",
                value,
                reviewed_input_sha256=digest,
                reviewed_by="Fixture Reviewer",
                approval_ref=reference,
            )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "position", ["root", "ancestor", "tenant", "revisions", "pointer", "spec", "receipt", "lock"]
)
def test_symlinks_rejected_at_every_publication_boundary(tmp_path: Path, position: str) -> None:
    root = tmp_path / "store"
    outside = tmp_path / "outside"
    outside.mkdir()
    if position == "root":
        root.symlink_to(outside, target_is_directory=True)
    elif position == "ancestor":
        root.symlink_to(outside, target_is_directory=True)
        root = root / "nested"
    else:
        receipt = _publish(root, _spec())
        target = {
            "tenant": root / "alpha",
            "revisions": root / "alpha/revisions",
            "pointer": root / "alpha/current.json",
            "spec": root / f"alpha/revisions/{receipt['sha256']}/spec.yaml",
            "receipt": root / f"alpha/revisions/{receipt['sha256']}/receipt.json",
            "lock": root / "alpha/.publication.lock",
        }[position]
        if position in {"tenant", "revisions"}:
            target.rename(target.with_name(target.name + "-old"))
            target.symlink_to(outside, target_is_directory=True)
        else:
            os.chmod(target.parent, 0o755)
            target.unlink()
            target.symlink_to(outside / "missing")
    with pytest.raises(DomainPublicationError):
        _publish(root, _spec())
    assert list(outside.iterdir()) == []


def test_duplicate_cannot_replace_review_evidence(tmp_path: Path) -> None:
    value = _spec()
    receipt = _publish(tmp_path, value)
    with pytest.raises(DomainPublicationError, match="conflicting review"):
        workflow.publish_effective_spec(
            tmp_path,
            "alpha",
            value,
            reviewed_input_sha256=receipt["sha256"],
            reviewed_by="Different Fixture Reviewer",
            approval_ref="test-fixture-only/approval-1",
        )
    assert _current(tmp_path) == receipt["sha256"]


def test_failed_pointer_install_retains_prior_revision_and_retry_is_safe(
    tmp_path: Path, monkeypatch
) -> None:
    first = _publish(tmp_path, _spec())
    replace = os.replace

    def fail_pointer(*args, **kwargs):
        raise OSError("Injected pointer installation failure")

    monkeypatch.setattr(workflow.os, "replace", fail_pointer)
    second = _spec("1.1.0")
    with pytest.raises(DomainPublicationError):
        _publish(tmp_path, second, expected_previous_sha256=first["sha256"])
    assert _current(tmp_path) == first["sha256"]
    assert not list((tmp_path / "alpha").glob(".current-*.json"))
    monkeypatch.setattr(workflow.os, "replace", replace)
    receipt = _publish(tmp_path, second, expected_previous_sha256=first["sha256"])
    assert _current(tmp_path) == receipt["sha256"]
    assert len(list((tmp_path / "alpha/revisions").iterdir())) == 2


def test_corrupt_history_cannot_be_rolled_back(tmp_path: Path) -> None:
    first = _publish(tmp_path, _spec())
    second = _publish(tmp_path, _spec("1.1.0"), expected_previous_sha256=first["sha256"])
    path = tmp_path / f"alpha/revisions/{first['sha256']}/spec.yaml"
    path.chmod(0o644)
    path.write_bytes(_spec("9.9.9"))
    with pytest.raises(DomainPublicationError):
        workflow.rollback_effective_spec(
            tmp_path,
            "alpha",
            first["sha256"],
            expected_previous_sha256=second["sha256"],
        )
    assert _current(tmp_path) == second["sha256"]


def _proposal_and_decisions(source_bytes: bytes):
    digest = hashlib.sha256(source_bytes).hexdigest()
    field_binding = {
        "node_label": "Facility",
        "discovery_spec_sha256": digest,
        "ownership_contract_version": 1,
    }
    proposal = proposer.SchemaProposalSet(
        domain="alpha",
        tenant_id="alpha",
        discovery_spec_sha256=digest,
        ownership_contract_version=1,
        proposed_fields=[
            proposer.FieldProposal(field_name="dock_count", field_type="integer", **field_binding),
            proposer.FieldProposal(field_name="unreviewed_field", **field_binding),
            proposer.FieldProposal(field_name="refused_field", **field_binding),
        ],
        proposed_gates=[proposer.GateProposal(field_name="dock_count")],
        proposed_scoring_dimensions=[proposer.ScoringDimensionProposal(field_name="dock_count")],
        version_bump="1.1.0-discovered",
    )
    decisions = [
        proposer.ApprovalDecision(field_name="dock_count", node_label="Facility", approved=True),
        proposer.ApprovalDecision(
            field_name="refused_field", node_label="Facility", approved=False
        ),
    ]
    return proposal, decisions


async def test_approved_fields_publish_only_after_durable_audit(
    tmp_path: Path, monkeypatch
) -> None:
    discovery_bytes = _spec()
    discovery_digest = hashlib.sha256(discovery_bytes).hexdigest()
    base = yaml.safe_load(discovery_bytes)
    original = copy.deepcopy(base)
    proposal, decisions = _proposal_and_decisions(discovery_bytes)
    candidate = workflow.materialize_approved_spec(
        base,
        proposal,
        decisions,
        tenant_id="alpha",
        node_label="Facility",
        source_sha256=discovery_digest,
    )
    events = []
    publish = workflow.publish_effective_spec

    async def persist(**kwargs):
        events.append("audit-committed")
        assert not (tmp_path / "alpha/current.json").exists()
        assert kwargs["tenant_id"] == kwargs["domain"] == "alpha"
        assert {row["field_name"] for row in kwargs["decisions"]} == {"dock_count", "refused_field"}
        assert all(row["proposed_gate"] is None for row in kwargs["decisions"])
        assert all(row["node_label"] == "Facility" for row in kwargs["decisions"])
        assert all(row["discovery_spec_sha256"] == discovery_digest for row in kwargs["decisions"])
        assert all(row["ownership_contract_version"] == 1 for row in kwargs["decisions"])
        return kwargs["decisions"]

    def track_publish(*args, **kwargs):
        events.append("publication")
        return publish(*args, **kwargs)

    monkeypatch.setattr(workflow.pg_store, "persist_schema_proposal_batch", persist)
    monkeypatch.setattr(workflow, "publish_effective_spec", track_publish)
    receipt = await workflow.approve_and_publish(
        tmp_path,
        "alpha",
        base,
        proposal,
        decisions,
        node_label="Facility",
        batch_run_id="fixture-batch-1",
        reviewed_input_sha256=hashlib.sha256(candidate).hexdigest(),
        reviewed_by="Fixture Reviewer",
        approval_ref="fixture-only/batch-review",
        source_sha256=discovery_digest,
    )
    assert events == ["audit-committed", "publication"]
    config = DomainYamlReader(tmp_path, require_published=True).load("alpha")
    fields = config.node_schemas["Facility"].properties
    assert "dock_count" in fields
    assert "refused_field" not in fields
    assert "unreviewed_field" not in fields
    assert config.raw_spec["inference_rules"] == original["inference_rules"]
    assert config.raw_spec["semantic_registry"] == original["semantic_registry"]
    assert (
        config.raw_spec["ontology"]["nodes"][0]["enrichment_hints"]
        == original["ontology"]["nodes"][0]["enrichment_hints"]
    )
    assert config.source_sha256 == receipt["sha256"]
    assert receipt["approval_batches"] == ["fixture-batch-1"]
    assert base == original


async def test_audit_failure_cannot_activate_candidate(tmp_path: Path, monkeypatch) -> None:
    discovery_bytes = _spec()
    discovery_digest = hashlib.sha256(discovery_bytes).hexdigest()
    base = yaml.safe_load(discovery_bytes)
    proposal, decisions = _proposal_and_decisions(discovery_bytes)
    candidate = workflow.materialize_approved_spec(
        base,
        proposal,
        decisions,
        tenant_id="alpha",
        node_label="Facility",
        source_sha256=discovery_digest,
    )
    monkeypatch.setattr(
        workflow.pg_store,
        "persist_schema_proposal_batch",
        AsyncMock(side_effect=RuntimeError("fixture DB failure")),
    )
    with pytest.raises(RuntimeError, match="fixture DB failure"):
        await workflow.approve_and_publish(
            tmp_path,
            "alpha",
            base,
            proposal,
            decisions,
            node_label="Facility",
            batch_run_id="fixture-batch-1",
            reviewed_input_sha256=hashlib.sha256(candidate).hexdigest(),
            reviewed_by="Fixture Reviewer",
            approval_ref="fixture-only",
            source_sha256=discovery_digest,
        )
    assert not (tmp_path / "alpha/current.json").exists()


async def test_digest_failure_precedes_audit(tmp_path: Path, monkeypatch) -> None:
    audit = AsyncMock()
    monkeypatch.setattr(workflow.pg_store, "persist_schema_proposal_batch", audit)
    discovery_bytes = _spec()
    proposal, decisions = _proposal_and_decisions(discovery_bytes)
    with pytest.raises(DomainPublicationError, match="Reviewed input digest"):
        await workflow.approve_and_publish(
            tmp_path,
            "alpha",
            yaml.safe_load(discovery_bytes),
            proposal,
            decisions,
            node_label="Facility",
            batch_run_id="fixture-batch-1",
            reviewed_input_sha256="f" * 64,
            reviewed_by="Fixture Reviewer",
            approval_ref="fixture-only",
            source_sha256=hashlib.sha256(discovery_bytes).hexdigest(),
        )
    audit.assert_not_awaited()
    assert list(tmp_path.iterdir()) == []


async def test_all_refused_audit_has_no_publication(tmp_path: Path, monkeypatch) -> None:
    audit = AsyncMock(return_value=[object()])
    monkeypatch.setattr(workflow.pg_store, "persist_schema_proposal_batch", audit)
    discovery_bytes = _spec()
    discovery_digest = hashlib.sha256(discovery_bytes).hexdigest()
    proposal = proposer.SchemaProposalSet(
        domain="alpha",
        tenant_id="alpha",
        discovery_spec_sha256=discovery_digest,
        ownership_contract_version=1,
        proposed_fields=[
            proposer.FieldProposal(
                field_name="dock_count",
                node_label="Facility",
                discovery_spec_sha256=discovery_digest,
                ownership_contract_version=1,
            )
        ],
    )
    decisions = [
        proposer.ApprovalDecision(field_name="dock_count", node_label="Facility", approved=False)
    ]
    base = yaml.safe_load(discovery_bytes)
    candidate = workflow.materialize_approved_spec(
        base,
        proposal,
        decisions,
        tenant_id="alpha",
        node_label="Facility",
        source_sha256=discovery_digest,
    )
    result = await workflow.approve_and_publish(
        tmp_path,
        "alpha",
        base,
        proposal,
        decisions,
        node_label="Facility",
        batch_run_id="fixture-batch-1",
        reviewed_input_sha256=hashlib.sha256(candidate).hexdigest(),
        reviewed_by="Fixture Reviewer",
        approval_ref="fixture-only",
        source_sha256=discovery_digest,
    )
    assert result is None
    audit.assert_awaited_once()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "alteration", ["missing_file", "wrong_reviewer", "bad_time", "duplicate_json", "self_previous"]
)
def test_corrupt_receipt_or_incomplete_revision_cannot_be_republished(
    tmp_path: Path, alteration: str
) -> None:
    value = _spec()
    receipt = _publish(tmp_path, value)
    revision = tmp_path / f"alpha/revisions/{receipt['sha256']}"
    os.chmod(revision, 0o755)
    if alteration == "missing_file":
        (revision / "receipt.json").unlink()
    else:
        path = revision / "receipt.json"
        path.chmod(0o644)
        changed = copy.deepcopy(receipt)
        if alteration == "wrong_reviewer":
            changed["reviewed_by"] = ""
        elif alteration == "bad_time":
            changed["reviewed_at"] = "2999-01-01T00:00:00+00:00"
        elif alteration == "self_previous":
            changed["previous_sha256"] = receipt["sha256"]
        if alteration == "duplicate_json":
            path.write_text('{"schema":"untrusted",' + json.dumps(changed)[1:])
        else:
            path.write_text(json.dumps(changed))
    with pytest.raises(DomainPublicationError):
        _publish(tmp_path, value)
    assert _current(tmp_path) == receipt["sha256"]


def test_failed_revision_write_never_installs_partial_content(tmp_path: Path, monkeypatch) -> None:
    first = _publish(tmp_path, _spec())
    original_write = workflow._write_file

    def failed_receipt(parent_fd, name, value, mode=0o444):
        if name == "receipt.json":
            raise OSError("Injected receipt write failure")
        return original_write(parent_fd, name, value, mode)

    monkeypatch.setattr(workflow, "_write_file", failed_receipt)
    with pytest.raises(DomainPublicationError):
        _publish(tmp_path, _spec("1.1.0"), expected_previous_sha256=first["sha256"])
    assert _current(tmp_path) == first["sha256"]
    assert [entry.name for entry in (tmp_path / "alpha/revisions").iterdir()] == [first["sha256"]]


def test_native_shaped_seed_marker_cannot_activate(tmp_path: Path) -> None:
    with pytest.raises(DomainPublicationError, match="Seed/draft"):
        _publish(tmp_path, _spec("0.1.0-seed"))
    assert not (tmp_path / "alpha/current.json").exists()


def test_rollback_unknown_digest_cannot_create_approval(tmp_path: Path) -> None:
    receipt = _publish(tmp_path, _spec())
    with pytest.raises(DomainPublicationError):
        workflow.rollback_effective_spec(
            tmp_path, "alpha", "f" * 64, expected_previous_sha256=receipt["sha256"]
        )
    assert _current(tmp_path) == receipt["sha256"]
    assert len(list((tmp_path / "alpha/revisions").iterdir())) == 1
