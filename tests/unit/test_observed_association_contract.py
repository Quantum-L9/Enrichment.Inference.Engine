"""
tests/unit/test_observed_association_contract.py

QM.POC0.TENANT_ASSOCIATION_MAPPING.004, EIE side: resolving a provider
association to the one tenant-declared relationship from EIE's mirror of the
tenant spec (``app/engines/observed_association_contract.py``), the mapping
fingerprint, and explicit drift detection against CEG's authoritative spec.

Every spec here is synthetic and built at test time; the provider token, object
types, category and type ids are placeholders, not any tenant's configuration.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.engines.convergence.schema_proposer import (
    ApprovalDecision,
    FieldProposal,
    SchemaProposalSet,
)
from app.engines.convergence.schema_proposer import apply as apply_proposals
from app.engines.observed_association_contract import (
    MAPPING_UNVERIFIED_MIRROR,
    ResolutionFailure,
    declared_ref_namespaces,
    mapping_fingerprint,
    mirror_drift,
    resolve_observed_association_contract,
)

pytestmark = pytest.mark.unit

ENTRY: dict[str, Any] = {
    "provider": "hubspot",
    "fromobjecttype": "companies",
    "toobjecttype": "deals",
    "associationcategory": "HUBSPOT_DEFINED",
    "associationtypeid": 342,
}
KEY = {
    "provider": "hubspot",
    "from_object_type": "companies",
    "to_object_type": "deals",
    "association_category": "HUBSPOT_DEFINED",
    "association_type_id": 342,
}


def _spec(*, observedfrom: Any = None) -> dict[str, Any]:
    return {
        "domain": {"id": "orbit-yard", "name": "synthetic", "version": "1.0.0-reviewed"},
        "ontology": {
            "nodes": [
                {"label": "OrbitalDepot", "properties": [{"name": "depot_id", "required": True}]},
                {
                    "label": "PropellantGrade",
                    "properties": [{"name": "grade_code", "required": True}],
                },
            ],
            "edges": [
                {
                    "type": "STOCKS",
                    "from": "OrbitalDepot",
                    "to": "PropellantGrade",
                    "direction": "DIRECTED",
                    "managedby": "sync",
                    "evidencerelevance": 0.5,
                }
            ],
        },
        "sync": {
            "endpoints": [
                {
                    "path": "/v1/sync/orbital-depots",
                    "targetnode": "OrbitalDepot",
                    "idproperty": "depot_id",
                    "batchstrategy": "UNWINDMERGE",
                    "refnamespace": "orbit.depot",
                },
                {
                    "path": "/v1/sync/propellant-grades",
                    "targetnode": "PropellantGrade",
                    "idproperty": "grade_code",
                    "batchstrategy": "UNWINDMERGE",
                    "refnamespace": "orbit.grade",
                },
                {
                    "path": "/v1/sync/stocks",
                    "targetedge": "STOCKS",
                    "fromnode": "OrbitalDepot",
                    "tonode": "PropellantGrade",
                    "batchstrategy": "UNWINDMERGE",
                    "observedfrom": [dict(ENTRY)] if observedfrom is None else observedfrom,
                },
            ]
        },
    }


def _endpoint(spec: dict[str, Any], path: str) -> dict[str, Any]:
    return next(e for e in spec["sync"]["endpoints"] if e["path"] == path)


def _resolve(spec: dict[str, Any], **overrides: Any) -> Any:
    return resolve_observed_association_contract(spec, **{**KEY, **overrides})


# ── E-T1: exactly one declared entry resolves, semantics from the endpoint ─────


def test_the_declared_association_resolves_to_the_endpoints_relationship() -> None:
    resolution = _resolve(_spec())
    contract = resolution.contract
    assert resolution.failure is None
    assert contract is not None
    assert (contract.sync.entity_type, contract.sync.relation_type) == ("stocks", "STOCKS")
    assert (contract.sync.from_label, contract.sync.to_label) == ("OrbitalDepot", "PropellantGrade")
    assert (contract.subject_ref_namespace, contract.object_ref_namespace) == (
        "orbit.depot",
        "orbit.grade",
    )
    assert contract.mapping_verification == MAPPING_UNVERIFIED_MIRROR
    assert contract.mapping_fingerprint == mapping_fingerprint(_spec())


def test_several_distinct_associations_may_feed_one_edge() -> None:
    spec = _spec(observedfrom=[dict(ENTRY), {**ENTRY, "associationtypeid": 343}])
    assert _resolve(spec, association_type_id=343).contract is not None
    assert _resolve(spec).contract is not None


# ── E-T2: no entry, duplicates, reversal, mismatch ─────────────────────────────


@pytest.mark.parametrize(
    ("overrides", "failure"),
    [
        ({"association_type_id": 341}, ResolutionFailure.UNDECLARED),
        ({"association_category": "USER_DEFINED"}, ResolutionFailure.UNDECLARED),
        (
            {"from_object_type": "deals", "to_object_type": "companies"},
            ResolutionFailure.UNDECLARED,
        ),
        ({"provider": "salesforce"}, ResolutionFailure.UNDECLARED),
        ({"from_object_type": "Companies"}, ResolutionFailure.UNDECLARED),
    ],
    ids=["type-id", "category", "reversed", "other-provider", "case"],
)
def test_an_undeclared_association_does_not_resolve(
    overrides: dict[str, Any], failure: ResolutionFailure
) -> None:
    resolution = _resolve(_spec(), **overrides)
    assert (resolution.contract, resolution.failure) == (None, failure)


def test_an_association_declared_twice_is_ambiguous() -> None:
    spec = _spec()
    second = copy.deepcopy(_endpoint(spec, "/v1/sync/stocks"))
    second["path"] = "/v1/sync/stocks-again"
    spec["sync"]["endpoints"].append(second)
    assert _resolve(spec).failure is ResolutionFailure.AMBIGUOUS
    assert (
        _resolve(_spec(observedfrom=[dict(ENTRY), dict(ENTRY)])).failure
        is ResolutionFailure.AMBIGUOUS
    )


# ── E-T3: any malformed entry anywhere refuses every association ───────────────


@pytest.mark.parametrize(
    "observedfrom",
    [
        [{**ENTRY, "relationtype": "STOCKS"}],
        [{**ENTRY, "associationtypeid": True}],
        [{**ENTRY, "associationtypeid": "342"}],
        [{**ENTRY, "associationtypeid": 0}],
        [{**ENTRY, "associationcategory": "hubspot_defined"}],
        [{**ENTRY, "toobjecttype": "deals/../x"}],
        [{k: v for k, v in ENTRY.items() if k != "provider"}],
        [],
        dict(ENTRY),
        ["not-a-mapping"],
    ],
    ids=[
        "unknown-key",
        "bool-id",
        "quoted-id",
        "zero-id",
        "category-case",
        "type-path",
        "missing-key",
        "empty-list",
        "not-a-list",
        "not-a-mapping",
    ],
)
def test_a_malformed_declaration_refuses_every_association(observedfrom: Any) -> None:
    spec = _spec()
    # The malformed block sits on a SECOND endpoint; the well-formed one still cannot resolve.
    spec["sync"]["endpoints"].append(
        {
            **copy.deepcopy(_endpoint(spec, "/v1/sync/stocks")),
            "path": "/v1/sync/other",
            "observedfrom": observedfrom,
        }
    )
    assert _resolve(spec).failure is ResolutionFailure.MALFORMED_DECLARATION


@pytest.mark.parametrize(
    "change",
    [
        {"/v1/sync/propellant-grades": {"refnamespace": "orbit.depot"}},
        {"/v1/sync/propellant-grades": {"refnamespace": "Orbit Grade"}},
        {"/v1/sync/stocks": {"refnamespace": "orbit.edge"}},
    ],
    ids=["shared-namespace", "malformed-namespace", "namespace-on-edge"],
)
def test_conflicting_or_malformed_namespaces_refuse_every_association(
    change: dict[str, Any],
) -> None:
    spec = _spec()
    for path, update in change.items():
        _endpoint(spec, path).update(update)
    assert _resolve(spec).failure is ResolutionFailure.MALFORMED_DECLARATION


def test_a_label_whose_endpoints_disagree_on_namespace_is_malformed() -> None:
    spec = _spec()
    spec["sync"]["endpoints"].append(
        {
            "path": "/v1/sync/depots-patch",
            "targetnode": "OrbitalDepot",
            "idproperty": "depot_id",
            "batchstrategy": "UNWINDMATCHSET",
        }
    )
    assert declared_ref_namespaces(spec) is None
    assert _resolve(spec).failure is ResolutionFailure.MALFORMED_DECLARATION


def test_an_observed_label_without_a_namespace_does_not_resolve() -> None:
    spec = _spec()
    del _endpoint(spec, "/v1/sync/propellant-grades")["refnamespace"]
    assert _resolve(spec).failure is ResolutionFailure.NAMESPACE_UNDECLARED


# ── E-T4: the owning endpoint must be one valid relationship endpoint ──────────


@pytest.mark.parametrize(
    "edge_change",
    [
        {"direction": "UNDIRECTED"},
        {"managedby": "api"},
        {"from": "PropellantGrade", "to": "OrbitalDepot"},
        {"type": "SUPPLIES"},
    ],
    ids=["undirected", "not-sync-managed", "reversed", "undeclared"],
)
def test_an_invalid_owning_edge_does_not_resolve(edge_change: dict[str, Any]) -> None:
    spec = _spec()
    spec["ontology"]["edges"][0].update(edge_change)
    assert _resolve(spec).failure is ResolutionFailure.INVALID_ENDPOINT


def test_an_entry_on_a_node_endpoint_does_not_resolve() -> None:
    spec = _spec()
    # An un-namespaced label, so only the relationship-endpoint rule can refuse it.
    _endpoint(spec, "/v1/sync/stocks")["targetnode"] = "UnrelatedLabel"
    assert _resolve(spec).failure is ResolutionFailure.INVALID_ENDPOINT


# ── Fingerprint and explicit drift against CEG's authoritative spec ────────────


def test_the_fingerprint_is_stable_and_ignores_unrelated_and_relevance_changes() -> None:
    base = mapping_fingerprint(_spec())
    reordered = _spec()
    reordered["sync"]["endpoints"].reverse()
    unrelated = _spec()
    unrelated["domain"]["version"] = "1.1.0-production"
    unrelated["ontology"]["edges"][0]["evidencerelevance"] = 0.9
    assert mapping_fingerprint(reordered) == base
    assert mapping_fingerprint(unrelated) == base
    changed = _spec(observedfrom=[{**ENTRY, "associationtypeid": 343}])
    assert mapping_fingerprint(changed) != base


def test_a_mirror_in_lockstep_shows_no_drift() -> None:
    assert mirror_drift(_spec(), _spec()) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: _endpoint(s, "/v1/sync/stocks")["observedfrom"].append(
            {**ENTRY, "associationtypeid": 9}
        ),
        lambda s: _endpoint(s, "/v1/sync/stocks")["observedfrom"][0].update(associationtypeid=341),
        lambda s: _endpoint(s, "/v1/sync/orbital-depots").update(refnamespace="orbit.other"),
        lambda s: s["ontology"]["edges"][0].update(direction="UNDIRECTED"),
        lambda s: _endpoint(s, "/v1/sync/stocks").pop("observedfrom"),
    ],
    ids=[
        "extra-association",
        "changed-type-id",
        "changed-namespace",
        "changed-edge",
        "removed-block",
    ],
)
def test_any_mapping_divergence_from_ceg_is_reported(mutate: Any) -> None:
    mirror = _spec()
    mutate(mirror)
    drift = mirror_drift(mirror, _spec())
    assert drift, "divergence not detected"
    assert any("only in EIE mirror" in line or "only in CEG spec" in line for line in drift)


# ── E-T12: automation never authors the mapping ────────────────────────────────


def test_schema_proposal_apply_never_adds_or_changes_sync() -> None:
    current = _spec()
    proposals = SchemaProposalSet(
        proposed_fields=[
            FieldProposal(
                field_name="dock_count",
                field_type="int",
                source="enrichment",
                fill_rate=0.9,
                avg_confidence=0.8,
            )
        ],
        version_bump="1.1.0-discovered",
        yaml_diff="+ dock_count: int",
    )
    updated = apply_proposals(
        current, [ApprovalDecision(field_name="dock_count", approved=True)], proposals
    )
    assert updated["sync"] == current["sync"]
    assert mapping_fingerprint(updated) == mapping_fingerprint(current)
