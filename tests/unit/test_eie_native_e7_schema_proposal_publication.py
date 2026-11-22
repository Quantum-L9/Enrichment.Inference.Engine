"""
tests/unit/test_eie_native_e7_schema_proposal_publication.py

Campaign EIE-NATIVE-CLOSURE-001, lane L4, slice E7, findings F15, F20, F14
(and P08 / F21). Acceptance cases P01..P08 for the schema-proposal ->
reviewed publication path:

- ``app/engines/convergence/schema_proposer.py`` (propose / apply, pure)
- ``app/services/domain_spec_workflow.py`` (materialize, approve_and_publish,
  publish_effective_spec: immutable revision + CAS ``current.json``)

Every store here is a disposable ``tmp_path`` directory. The Postgres audit
(``pg_store.persist_schema_proposal_batch``) is replaced by a recording fake;
no service is reached. The tenant spec is a domain-neutral synthetic with a
list-shaped ontology; no file under ``domains/`` is read or written.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from app.engines.convergence import schema_proposer as proposer
from app.engines.domain_yaml_reader import (
    DomainPublicationError,
    DomainYamlReader,
    validate_native_spec,
)
from app.services import domain_spec_workflow as workflow

pytestmark = pytest.mark.unit

TENANT = "synthetic-yard"
OWNER = "Holder"
OTHER = "Item"
REVIEWER = "Fixture Reviewer"
APPROVAL = "test-fixture-only/approval"


def _base(version: str = "1.0.0") -> dict[str, Any]:
    return {
        "name": TENANT,
        "version": version,
        "ontology": {
            "nodes": [
                {
                    "label": OWNER,
                    "properties": [
                        {"name": "holder_id", "type": "string"},
                        {"name": "title", "type": "string"},
                    ],
                },
                {
                    "label": OTHER,
                    "properties": [
                        {"name": "item_id", "type": "string"},
                        {"name": "grade", "type": "string"},
                    ],
                },
            ]
        },
        "inference_rules": [],
        "semantic_registry": {"enabled": False},
        "subjects": {
            "roles": ["keeper"],
            "types": [{"name": "holding", "node": OWNER, "fields": ["title"]}],
        },
    }


def _bytes(spec: dict[str, Any]) -> bytes:
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _proposal(
    base: dict[str, Any],
    fields: list[tuple[str, str]] | None = None,
    *,
    with_ranking: bool = False,
) -> proposer.SchemaProposalSet:
    digest = _sha(_bytes(base))
    proposed = [
        proposer.FieldProposal(
            field_name=name,
            field_type="integer",
            node_label=owner,
            discovery_spec_sha256=digest,
            ownership_contract_version=1,
        )
        for owner, name in (fields or [(OWNER, "slot_count")])
    ]
    return proposer.SchemaProposalSet(
        domain=TENANT,
        tenant_id=TENANT,
        discovery_spec_sha256=digest,
        ownership_contract_version=1,
        proposed_fields=proposed,
        proposed_gates=(
            [proposer.GateProposal(field_name=proposed[0].field_name)] if with_ranking else []
        ),
        proposed_scoring_dimensions=(
            [proposer.ScoringDimensionProposal(field_name=proposed[0].field_name)]
            if with_ranking
            else []
        ),
        version_bump="1.1.0-discovered",
    )


def _decide(*pairs: tuple[str, str, bool]) -> list[proposer.ApprovalDecision]:
    return [
        proposer.ApprovalDecision(node_label=owner, field_name=name, approved=approved)
        for owner, name, approved in pairs
    ]


class _Audit:
    """Recording fake of the Postgres audit; it commits whatever it is given."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return list(kwargs["decisions"])


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> _Audit:
    fake = _Audit()
    monkeypatch.setattr(workflow.pg_store, "persist_schema_proposal_batch", fake)
    return fake


def _materialize(
    base: dict[str, Any],
    proposal: proposer.SchemaProposalSet,
    decisions: list[proposer.ApprovalDecision],
    **kwargs: Any,
) -> bytes:
    kwargs.setdefault("source_sha256", _sha(_bytes(base)))
    return workflow.materialize_approved_spec(base, proposal, decisions, tenant_id=TENANT, **kwargs)


async def _approve(
    root: Path,
    base: dict[str, Any],
    proposal: proposer.SchemaProposalSet,
    decisions: list[proposer.ApprovalDecision],
    *,
    reviewed: str | None = None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    source = kwargs.pop("source_sha256", _sha(_bytes(base)))
    if reviewed is None:
        reviewed = _sha(_materialize(base, proposal, decisions, source_sha256=source))
    return await workflow.approve_and_publish(
        root,
        TENANT,
        base,
        proposal,
        decisions,
        batch_run_id="fixture-batch-1",
        reviewed_input_sha256=reviewed,
        reviewed_by=REVIEWER,
        approval_ref=APPROVAL,
        source_sha256=source,
        **kwargs,
    )


def _publish_base(root: Path, base: dict[str, Any]) -> str:
    data = _bytes(base)
    receipt = workflow.publish_effective_spec(
        root,
        TENANT,
        data,
        reviewed_input_sha256=_sha(data),
        reviewed_by=REVIEWER,
        approval_ref=APPROVAL,
    )
    return str(receipt["sha256"])


def _current(root: Path) -> str | None:
    pointer = root / TENANT / "current.json"
    return json.loads(pointer.read_text())["sha256"] if pointer.exists() else None


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != ".lock"
    }


def _node(spec: dict[str, Any], label: str) -> dict[str, Any]:
    (node,) = [n for n in spec["ontology"]["nodes"] if n["label"] == label]
    return node


def test_fixture_base_is_a_valid_native_spec() -> None:
    assert validate_native_spec(_base(), TENANT) == _base()


# ── P01 [F15]: no approved proposal => no revision, no pointer change ───────


@pytest.mark.parametrize(
    "decisions",
    [[], _decide((OWNER, "slot_count", False))],
    ids=["undecided", "all-refused"],
)
async def test_p01_no_approval_publishes_nothing_and_keeps_the_pointer(
    tmp_path: Path, audit: _Audit, decisions: list[proposer.ApprovalDecision]
) -> None:
    base = _base()
    previous = _publish_base(tmp_path, base)
    before = _snapshot(tmp_path)

    assert await _approve(tmp_path, base, _proposal(base), decisions) is None

    assert _current(tmp_path) == previous
    assert _snapshot(tmp_path) == before  # no revision directory, no receipt
    applied = proposer.apply(base, decisions, _proposal(base), source_sha256=_sha(_bytes(base)))
    assert applied["version"] == base["version"]  # no version bump without approval
    assert _node(applied, OWNER) == _node(base, OWNER)


# ── P02 [F20]: one approved node-scoped property, list-shaped ontology ──────


def test_p02_only_the_approved_property_is_added_with_a_deterministic_version() -> None:
    base = _base()
    original = copy.deepcopy(base)
    proposal = _proposal(base, [(OWNER, "slot_count"), (OWNER, "unreviewed_field")])
    decisions = _decide((OWNER, "slot_count", True))

    first = _materialize(base, proposal, decisions)
    second = _materialize(copy.deepcopy(base), proposal, decisions)
    assert first == second  # deterministic candidate bytes

    candidate = yaml.safe_load(first)
    assert candidate["version"] == "1.1.0-discovered"
    added = [p["name"] for p in _node(candidate, OWNER)["properties"]]
    assert added == ["holder_id", "title", "slot_count"]
    assert "unreviewed_field" not in first.decode()
    unchanged = {k: v for k, v in candidate.items() if k not in {"ontology", "version"}}
    assert unchanged == {k: v for k, v in original.items() if k not in {"ontology", "version"}}
    assert base == original  # the base is never mutated


# ── P03 [F20]: two nodes, proposal owned by one ─────────────────────────────


async def test_p03_unrelated_node_untouched_and_owner_recorded(
    tmp_path: Path, audit: _Audit
) -> None:
    base = _base()
    previous = _publish_base(tmp_path, base)
    proposal = _proposal(base)
    receipt = await _approve(
        tmp_path,
        base,
        proposal,
        _decide((OWNER, "slot_count", True)),
        expected_previous_sha256=previous,
    )
    assert receipt is not None
    assert receipt["previous_sha256"] == previous
    assert _current(tmp_path) == receipt["sha256"]

    served = DomainYamlReader(tmp_path, require_published=True).load(TENANT).raw_spec
    assert _node(served, OTHER) == _node(base, OTHER)
    assert "slot_count" in [p["name"] for p in _node(served, OWNER)["properties"]]
    (call,) = audit.calls
    assert call["tenant_id"] == call["domain"] == TENANT
    assert [(row["node_label"], row["field_name"]) for row in call["decisions"]] == [
        (OWNER, "slot_count")
    ]
    assert call["decisions"][0]["discovery_spec_sha256"] == _sha(_bytes(base))


# ── P04 [F20]: conflicting decision / owner => atomic refusal ───────────────


@pytest.mark.parametrize(
    ("decisions", "match"),
    [
        # approval names a node that does not own the proposed field
        (_decide((OTHER, "slot_count", True)), "unproposed owner/field pair"),
        (
            _decide((OWNER, "slot_count", True), (OWNER, "slot_count", False)),
            "Duplicate approval owner/field pair",
        ),
        (
            _decide((OWNER, "slot_count", True), (OWNER, "never_proposed", True)),
            "unproposed owner/field pair",
        ),
    ],
    ids=["wrong-owner", "contradictory", "unproposed-pair"],
)
async def test_p04_conflicting_decisions_refuse_the_whole_batch(
    tmp_path: Path,
    audit: _Audit,
    decisions: list[proposer.ApprovalDecision],
    match: str,
) -> None:
    base = _base()
    previous = _publish_base(tmp_path, base)
    before = _snapshot(tmp_path)
    proposal = _proposal(base)  # slot_count is proposed for OWNER only
    # The digest of the valid single approval: the review-digest guard would pass,
    # so only decision validation can refuse the batch.
    valid = _sha(_materialize(base, proposal, _decide((OWNER, "slot_count", True))))
    with pytest.raises(DomainPublicationError, match=match):
        await _approve(tmp_path, base, proposal, decisions, reviewed=valid)
    assert audit.calls == []  # nothing audited
    assert _snapshot(tmp_path) == before  # nothing published, partially or otherwise
    assert _current(tmp_path) == previous


async def test_p04_operator_label_cannot_redirect_the_recorded_owner(
    tmp_path: Path, audit: _Audit
) -> None:
    base = _base()
    with pytest.raises(DomainPublicationError):
        _materialize(base, _proposal(base), _decide((OWNER, "slot_count", True)), node_label=OTHER)
    assert audit.calls == []
    assert list(tmp_path.iterdir()) == []


# ── P05 [F20]: stale discovery digest / unexpected source hash ──────────────


async def test_p05_stale_discovery_digest_is_refused_and_pointer_unchanged(
    tmp_path: Path, audit: _Audit
) -> None:
    base = _base()
    previous = _publish_base(tmp_path, base)
    before = _snapshot(tmp_path)
    proposal = _proposal(base)
    moved = _base("1.0.1")  # the discovery source changed after the proposal was made
    with pytest.raises(DomainPublicationError, match="Stale discovery spec digest"):
        await _approve(
            tmp_path,
            moved,
            proposal,
            _decide((OWNER, "slot_count", True)),
            reviewed="f" * 64,
            source_sha256=_sha(_bytes(moved)),
        )
    assert audit.calls == []
    assert _snapshot(tmp_path) == before
    assert _current(tmp_path) == previous


async def test_p05_conflicting_digest_assertions_are_refused(tmp_path: Path, audit: _Audit) -> None:
    base = _base()
    with pytest.raises(DomainPublicationError, match="Conflicting discovery source digest"):
        _materialize(
            base,
            _proposal(base),
            _decide((OWNER, "slot_count", True)),
            source_sha256=_sha(_bytes(base)),
            source_digest="0" * 64,
        )
    assert list(tmp_path.iterdir()) == []


async def test_p05_unexpected_previous_hash_is_refused_and_pointer_unchanged(
    tmp_path: Path, audit: _Audit
) -> None:
    base = _base()
    previous = _publish_base(tmp_path, base)
    revisions_before = sorted(p.name for p in (tmp_path / TENANT / "revisions").iterdir())
    with pytest.raises(DomainPublicationError):
        await _approve(
            tmp_path,
            base,
            _proposal(base),
            _decide((OWNER, "slot_count", True)),
            expected_previous_sha256="e" * 64,
        )
    assert _current(tmp_path) == previous
    served = DomainYamlReader(tmp_path, require_published=True).load(TENANT)
    assert served.source_sha256 == previous
    # A retained immutable revision may be staged, but it is never current.
    assert set(revisions_before) <= {p.name for p in (tmp_path / TENANT / "revisions").iterdir()}


# ── P06 [F15]: candidate digest mismatches the supplied approval ────────────


async def test_p06_review_digest_mismatch_is_refused_and_previous_still_served(
    tmp_path: Path, audit: _Audit
) -> None:
    base = _base()
    previous = _publish_base(tmp_path, base)
    before = _snapshot(tmp_path)
    other_candidate = _materialize(
        base, _proposal(base, [(OWNER, "other_field")]), _decide((OWNER, "other_field", True))
    )
    with pytest.raises(DomainPublicationError, match="Reviewed input digest"):
        await _approve(
            tmp_path,
            base,
            _proposal(base),
            _decide((OWNER, "slot_count", True)),
            reviewed=_sha(other_candidate),  # an approval of different bytes
            expected_previous_sha256=previous,
        )
    assert audit.calls == []
    assert _snapshot(tmp_path) == before
    served = DomainYamlReader(tmp_path, require_published=True).load(TENANT)
    assert served.source_sha256 == previous
    assert "slot_count" not in served.node_schemas[OWNER].properties


def test_p06_publication_refuses_bytes_other_than_the_reviewed_ones(tmp_path: Path) -> None:
    data = _bytes(_base())
    with pytest.raises(DomainPublicationError, match="Reviewed input digest"):
        workflow.publish_effective_spec(
            tmp_path,
            TENANT,
            data,
            reviewed_input_sha256=_sha(data + b"\n# edited after review\n"),
            reviewed_by=REVIEWER,
            approval_ref=APPROVAL,
        )
    assert _current(tmp_path) is None


# ── P07 [F14]: approved field outside the subjects grouping ─────────────────


def test_p07_subjects_grouping_is_not_guessed_by_the_workflow() -> None:
    """Authority: ``domain_spec_schema._subjects`` checks ``fields`` for form only;
    the proposal workflow does not maintain the grouping and membership is pinned
    per cartridge by that cartridge's contract test. Converge request v2.0 has no
    subject-to-fields expansion (docs/contracts/converge/request.v2.md 3.5/3.6),
    so no runtime path depends on completeness. The workflow therefore leaves the
    grouping exactly as reviewed and assigns the new field to no concern."""
    base = _base()
    candidate = yaml.safe_load(
        _materialize(base, _proposal(base), _decide((OWNER, "slot_count", True)))
    )
    assert "slot_count" in [p["name"] for p in _node(candidate, OWNER)["properties"]]
    assert candidate["subjects"] == base["subjects"]
    assert validate_native_spec(candidate, TENANT) == candidate


# ── P08 [F21]: a proposal cannot mutate CEG gates or ranking ────────────────


async def test_p08_ranking_proposals_never_reach_the_spec_or_the_audit(
    tmp_path: Path, audit: _Audit
) -> None:
    base = _base()
    proposal = _proposal(base, with_ranking=True)
    assert proposal.proposed_gates and proposal.proposed_scoring_dimensions
    receipt = await _approve(tmp_path, base, proposal, _decide((OWNER, "slot_count", True)))
    assert receipt is not None
    served = DomainYamlReader(tmp_path, require_published=True).load(TENANT).raw_spec
    for key in ("gates", "ranking", "ranking_rules", "scoring", "scoring_dimensions"):
        assert not served.get(key), key
    (call,) = audit.calls
    assert all(row["proposed_gate"] is None for row in call["decisions"])
    assert all(row["proposed_scoring_dimension"] is None for row in call["decisions"])


def test_p08_propose_emits_no_gate_or_ranking_dimension() -> None:
    base = _base()
    result = proposer.propose(
        [
            {
                "node_label": OWNER,
                "final_fields": {"slot_count": 3},
                "final_field_confidences": {
                    "slot_count": {"confidence": 0.9, "source": "inference"}
                },
            }
        ],
        base,
        domain=TENANT,
        tenant_id=TENANT,
        discovery_spec_sha256=_sha(_bytes(base)),
    )
    assert [(fp.node_label, fp.field_name) for fp in result.proposed_fields] == [
        (OWNER, "slot_count")
    ]
    assert result.proposed_gates == []
    assert result.proposed_scoring_dimensions == []


@pytest.mark.parametrize(
    "section",
    [
        {"gates": [{"field": "slot_count"}]},
        {"ranking": {"weights": {"slot_count": 1.0}}},
        {"scoring": {"dimensions": [{"field": "slot_count"}]}},
    ],
    ids=["gates", "ranking", "scoring"],
)
def test_p08_a_ceg_owned_section_is_refused_by_the_owner_check(
    tmp_path: Path, section: dict[str, Any]
) -> None:
    spec = {**_base(), **section}
    data = _bytes(spec)
    with pytest.raises((DomainPublicationError, ValueError)):
        workflow.publish_effective_spec(
            tmp_path,
            TENANT,
            data,
            reviewed_input_sha256=_sha(data),
            reviewed_by=REVIEWER,
            approval_ref=APPROVAL,
        )
    assert _current(tmp_path) is None


# ── F20: list-shaped replay of approved audit rows (apply_approved_records) ──


def _row(digest: str, **overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "tenant_id": TENANT,
        "domain": TENANT,
        "batch_run_id": "run-1",
        "node_label": OWNER,
        "discovery_spec_sha256": digest,
        "ownership_contract_version": 1,
        "field_name": "slot_count",
        "field_type": "integer",
        "source": "enrichment",
        "fill_rate": 1.0,
        "avg_confidence": 0.9,
        "sample_values": [],
        "value_distribution": {},
        "proposed_gate": None,
        "proposed_scoring_dimension": None,
        "approval_status": "approved",
        "reviewed_by": REVIEWER,
        "reviewed_at": datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _replay(spec: dict[str, Any], rows: list[Any], artifacts: dict[str, bytes]) -> dict[str, Any]:
    from app.api.v1 import converge

    return converge.apply_approved_records(spec, rows, discovery_specs=artifacts)


def test_p02_p03_replay_adds_only_the_approved_owner_property_with_one_bump() -> None:
    base = _base()
    discovery = _bytes(base)
    digest = _sha(discovery)
    original = copy.deepcopy(base)
    revised = _replay(base, [_row(digest)], {digest: discovery})
    assert revised["version"] == "1.1.0-discovered"
    assert [p["name"] for p in _node(revised, OWNER)["properties"]] == [
        "holder_id",
        "title",
        "slot_count",
    ]
    assert _node(revised, OTHER) == _node(original, OTHER)
    assert revised["subjects"] == original["subjects"]
    assert base == original  # replay returns a new spec; the input is untouched
    assert _replay(base, [_row(digest)], {digest: discovery}) == revised  # deterministic


@pytest.mark.parametrize(
    ("overrides", "artifacts", "match"),
    [
        ({}, "missing", "recorded discovery artifact"),
        ({}, "changed", "recorded discovery artifact|digest mismatch"),
        ({"tenant_id": "foreign-yard", "domain": "foreign-yard"}, "exact", "cross-tenant"),
        ({"approval_status": "pending"}, "exact", "approved audit records"),
        ({"reviewed_by": " "}, "exact", "named reviewer"),
        ({"node_label": "Absent"}, "exact", ""),
        ({"discovery_spec_sha256": "A" * 64}, "exact", "discovery_spec_sha256"),
    ],
    ids=[
        "artifact-missing",
        "artifact-changed",
        "foreign-tenant",
        "unapproved",
        "unnamed-reviewer",
        "unknown-owner",
        "malformed-digest",
    ],
)
def test_p05_replay_refuses_stale_or_unbound_rows_and_changes_nothing(
    overrides: dict[str, Any], artifacts: str, match: str
) -> None:
    base = _base()
    discovery = _bytes(base)
    digest = _sha(discovery)
    supplied = {
        "missing": {},
        "changed": {digest: _bytes(_base("1.0.1"))},
        "exact": {digest: discovery},
    }[artifacts]
    original = copy.deepcopy(base)
    with pytest.raises(ValueError, match=match or None):
        _replay(base, [_row(digest, **overrides)], supplied)
    assert base == original


def test_p04_replay_refuses_a_batch_binding_two_discovery_artifacts() -> None:
    base = _base()
    first, second = _bytes(base), _bytes(_base("1.0.1"))
    rows = [
        _row(_sha(first)),
        _row(_sha(second), field_name="other_field"),
    ]
    original = copy.deepcopy(base)
    with pytest.raises(ValueError, match="one discovery artifact"):
        _replay(base, rows, {_sha(first): first, _sha(second): second})
    assert base == original
