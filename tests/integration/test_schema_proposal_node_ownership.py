"""Owner-bound offline bridge integration with fake audit storage, no live DB.

L9_META:
  tier: 2
  authority: native-domain-completion-v3-g27r
"""

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

pytestmark = pytest.mark.integration


def _base():
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


def _bytes(base=None):
    return yaml.safe_dump(base if base is not None else _base(), sort_keys=False).encode()


def _digest(base=None):
    return hashlib.sha256(_bytes(base)).hexdigest()


def _row(owner="Facility", **changes):
    return SimpleNamespace(
        **{
            "tenant_id": "owner-fixture",
            "domain": "owner-fixture",
            "batch_run_id": "batch-1",
            "approval_status": "approved",
            "node_label": owner,
            "field_name": "shared_metric",
            "field_type": "float" if owner == "Facility" else "string",
            "source": "inference",
            "fill_rate": 0.9,
            "avg_confidence": 0.9,
            "reviewed_by": "Fixture Reviewer",
            "reviewed_at": datetime.now(UTC),
            "discovery_spec_sha256": _digest(),
            "ownership_contract_version": 1,
            **changes,
        }
    )


def _proposal(base=None):
    raw = base if base is not None else _base()
    return proposer.propose(
        [
            {
                "node_label": owner,
                "final_fields": {"shared_metric": value},
                "final_field_confidences": {"shared_metric": 0.9},
            }
            for owner, value in (("Facility", 1.0), ("Buyer", "A"))
        ],
        raw,
        "owner-fixture",
        tenant_id="owner-fixture",
        discovery_spec_sha256=_digest(raw),
    )


def _decisions(approved=True):
    return [
        proposer.ApprovalDecision(node_label=owner, field_name="shared_metric", approved=approved)
        for owner in ("Facility", "Buyer")
    ]


async def test_audited_restart_uses_recorded_owners_and_one_batch_bump(monkeypatch):
    rows = [_row(), _row("Buyer")]
    original_rows = copy.deepcopy(rows)
    audit = AsyncMock(return_value=rows)
    monkeypatch.setattr(workflow.pg_store, "get_approved_schema_proposals", audit)
    writes = AsyncMock()
    monkeypatch.setattr(workflow.pg_store, "persist_schema_proposal_batch", writes)
    keys = []
    candidate, batches = await workflow.materialize_audited_spec(
        _base(),
        tenant_id="owner-fixture",
        domain="owner-fixture",
        discovery_specs={_digest(): _bytes()},
        applied_field_keys=keys,
    )
    raw = yaml.safe_load(candidate)
    assert raw["version"] == "1.1.0-discovered"
    assert [n["properties"][-1]["type"] for n in raw["ontology"]["nodes"]] == ["float", "string"]
    assert batches == ["batch-1"]
    assert keys == [("Facility", "shared_metric"), ("Buyer", "shared_metric")]
    writes.assert_not_awaited()
    assert [vars(r) for r in rows] == [vars(r) for r in original_rows]
    second, second_batches = await workflow.materialize_audited_spec(
        raw,
        tenant_id="owner-fixture",
        domain="owner-fixture",
        discovery_specs={_digest(): _bytes()},
    )
    assert second == candidate
    assert second_batches == []


async def test_operator_label_is_assertion_and_cannot_drop_mixed_owner_subset(monkeypatch):
    monkeypatch.setattr(
        workflow.pg_store,
        "get_approved_schema_proposals",
        AsyncMock(return_value=[_row(), _row("Buyer")]),
    )
    with pytest.raises(DomainPublicationError, match="node_label"):
        await workflow.materialize_audited_spec(
            _base(),
            tenant_id="owner-fixture",
            domain="owner-fixture",
            node_label="Buyer",
            batch_run_ids=["batch-1"],
            discovery_specs={_digest(): _bytes()},
        )


@pytest.mark.parametrize("selection", [[], ["batch-1"]])
async def test_selected_and_default_unbound_legacy_rows_refuse_without_mutation(
    monkeypatch, selection
):
    row = _row(node_label=None, discovery_spec_sha256=None, ownership_contract_version=None)
    original = copy.deepcopy(vars(row))
    monkeypatch.setattr(
        workflow.pg_store, "get_approved_schema_proposals", AsyncMock(return_value=[row])
    )
    with pytest.raises(DomainPublicationError, match="bound"):
        await workflow.materialize_audited_spec(
            _base(),
            tenant_id="owner-fixture",
            domain="owner-fixture",
            node_label="Facility",
            batch_run_ids=selection,
            discovery_specs={_digest(): _bytes()},
        )
    assert vars(row) == original


@pytest.mark.parametrize(
    "evidence", [None, {}, {"f" * 64: _bytes()}, {_digest(): b"name: forged\n"}]
)
async def test_missing_or_changed_exact_discovery_artifact_refuses(monkeypatch, evidence):
    monkeypatch.setattr(
        workflow.pg_store, "get_approved_schema_proposals", AsyncMock(return_value=[_row()])
    )
    with pytest.raises(DomainPublicationError, match="discovery"):
        await workflow.materialize_audited_spec(
            _base(), tenant_id="owner-fixture", domain="owner-fixture", discovery_specs=evidence
        )


async def test_rebase_verifies_owner_in_original_artifact_before_new_base(monkeypatch):
    original = _base()
    original["ontology"]["nodes"].pop()
    digest = _digest(original)
    monkeypatch.setattr(
        workflow.pg_store,
        "get_approved_schema_proposals",
        AsyncMock(return_value=[_row("Buyer", discovery_spec_sha256=digest)]),
    )
    with pytest.raises(DomainPublicationError, match="node_label"):
        await workflow.materialize_audited_spec(
            _base(),
            tenant_id="owner-fixture",
            domain="owner-fixture",
            discovery_specs={digest: _bytes(original)},
        )


async def test_rebase_owner_scoped_same_type_and_conflict_behavior(monkeypatch):
    raw = _base()
    raw["version"] = "1.2.0"
    raw["ontology"]["nodes"][0]["properties"].append({"name": "shared_metric", "type": "float"})
    monkeypatch.setattr(
        workflow.pg_store,
        "get_approved_schema_proposals",
        AsyncMock(return_value=[_row(), _row("Buyer")]),
    )
    candidate, _ = await workflow.materialize_audited_spec(
        raw,
        tenant_id="owner-fixture",
        domain="owner-fixture",
        discovery_specs={_digest(): _bytes()},
    )
    assert yaml.safe_load(candidate)["version"] == "1.3.0-discovered"
    raw["ontology"]["nodes"][0]["properties"][-1]["type"] = "integer"
    with pytest.raises(DomainPublicationError, match="different type"):
        await workflow.materialize_audited_spec(
            raw,
            tenant_id="owner-fixture",
            domain="owner-fixture",
            discovery_specs={_digest(): _bytes()},
        )


@pytest.mark.parametrize(
    "alteration", ["duplicate", "wrong-tenant", "missing-review", "future-review", "bad-version"]
)
async def test_bad_audit_batch_fails_before_diagnostics_or_candidate(monkeypatch, alteration):
    rows = [_row(), _row("Buyer")]
    if alteration == "duplicate":
        rows.append(rows[0])
    elif alteration == "wrong-tenant":
        rows[1].tenant_id = "other"
    elif alteration == "missing-review":
        rows[1].reviewed_by = None
    elif alteration == "future-review":
        rows[1].reviewed_at = datetime(2099, 1, 1, tzinfo=UTC)
    else:
        rows[1].ownership_contract_version = 2
    monkeypatch.setattr(
        workflow.pg_store, "get_approved_schema_proposals", AsyncMock(return_value=rows)
    )
    keys = []
    with pytest.raises(DomainPublicationError):
        await workflow.materialize_audited_spec(
            _base(),
            tenant_id="owner-fixture",
            domain="owner-fixture",
            discovery_specs={_digest(): _bytes()},
            applied_field_keys=keys,
        )
    assert keys == []


@pytest.mark.parametrize("approved_owner", ["Facility", "Buyer"])
async def test_direct_mixed_pair_review_audits_owner_identity_before_publication(
    tmp_path, monkeypatch, approved_owner
):
    base = _base()
    proposal = _proposal()
    decisions = [
        d.model_copy(update={"approved": d.node_label == approved_owner}) for d in _decisions()
    ]
    candidate = workflow.materialize_approved_spec(
        base, proposal, decisions, tenant_id="owner-fixture", source_digest=_digest()
    )
    events = []

    async def persist(**kwargs):
        events.append("audit")
        assert kwargs["tenant_id"] == kwargs["domain"] == "owner-fixture"
        assert {(r["node_label"], r["field_name"]) for r in kwargs["decisions"]} == {
            ("Facility", "shared_metric"),
            ("Buyer", "shared_metric"),
        }
        assert all(
            r["discovery_spec_sha256"] == _digest() and r["ownership_contract_version"] == 1
            for r in kwargs["decisions"]
        )
        return kwargs["decisions"]

    monkeypatch.setattr(workflow.pg_store, "persist_schema_proposal_batch", persist)
    receipt = await workflow.approve_and_publish(
        tmp_path,
        "owner-fixture",
        base,
        proposal,
        decisions,
        source_digest=_digest(),
        batch_run_id="batch-1",
        reviewed_input_sha256=hashlib.sha256(candidate).hexdigest(),
        reviewed_by="Fixture Reviewer",
        approval_ref="fixture-only",
    )
    assert events == ["audit"]
    assert receipt["approval_batches"] == ["batch-1"]
    assert (
        json.loads((tmp_path / "owner-fixture/current.json").read_bytes())["sha256"]
        == receipt["sha256"]
    )
    updated = yaml.safe_load(candidate)
    assert all(
        (len(n["properties"]) == 2) == (n["label"] == approved_owner)
        for n in updated["ontology"]["nodes"]
    )


@pytest.mark.parametrize("failure", ["stale", "unbound", "wrong-label", "review-digest"])
async def test_direct_preflight_refuses_before_any_audit_or_output(tmp_path, monkeypatch, failure):
    audit = AsyncMock()
    monkeypatch.setattr(workflow.pg_store, "persist_schema_proposal_batch", audit)
    proposal = _proposal()
    digest = _digest()
    label = None
    if failure == "stale":
        digest = "f" * 64
    elif failure == "unbound":
        proposal.proposed_fields[1].node_label = None
    elif failure == "wrong-label":
        label = "Buyer"
    with pytest.raises(DomainPublicationError):
        await workflow.approve_and_publish(
            tmp_path,
            "owner-fixture",
            _base(),
            proposal,
            _decisions(),
            source_digest=digest,
            node_label=label,
            batch_run_id="batch-1",
            reviewed_input_sha256="e" * 64,
            reviewed_by="Fixture Reviewer",
            approval_ref="fixture-only",
        )
    audit.assert_not_awaited()
    assert list(tmp_path.iterdir()) == []


async def test_all_rejected_bound_decisions_audit_without_publication(tmp_path, monkeypatch):
    audit = AsyncMock(return_value=[object(), object()])
    monkeypatch.setattr(workflow.pg_store, "persist_schema_proposal_batch", audit)
    candidate = workflow.materialize_approved_spec(
        _base(), _proposal(), _decisions(False), tenant_id="owner-fixture", source_digest=_digest()
    )
    result = await workflow.approve_and_publish(
        tmp_path,
        "owner-fixture",
        _base(),
        _proposal(),
        _decisions(False),
        source_digest=_digest(),
        batch_run_id="batch-1",
        reviewed_input_sha256=hashlib.sha256(candidate).hexdigest(),
        reviewed_by="Fixture Reviewer",
        approval_ref="fixture-only",
    )
    assert result is None
    audit.assert_awaited_once()
    assert list(tmp_path.iterdir()) == []


def test_operator_optional_label_and_exact_discovery_source_inputs(
    tmp_path: Path, monkeypatch, capsys
):
    base = tmp_path / "base.yaml"
    base.write_bytes(_bytes())
    output = tmp_path / "candidate.yaml"
    monkeypatch.setattr(
        workflow.pg_store,
        "get_approved_schema_proposals",
        AsyncMock(return_value=[_row(), _row("Buyer")]),
    )
    assert (
        operator.main(
            [
                "materialize-approved",
                "--base-spec",
                str(base),
                "--tenant",
                "owner-fixture",
                "--domain",
                "owner-fixture",
                "--discovery-spec",
                str(base),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["applied_field_keys"] == [
        ["Facility", "shared_metric"],
        ["Buyer", "shared_metric"],
    ]
    assert result["applied_approval_batches"] == ["batch-1"]
    assert not (tmp_path / "owner-fixture/current.json").exists()
