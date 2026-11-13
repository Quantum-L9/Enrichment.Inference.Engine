"""
Tenant-declared provider association mapping, read from EIE's mirror of the tenant spec.

QM.POC0.TENANT_ASSOCIATION_MAPPING.004. The authority is the tenant domain spec as
CEG validates it (``engine/config/tenant_mapping.py`` in Cognitive.Engine.Graphs):

- ``sync.endpoints[].observedfrom`` on a relationship endpoint names the provider
  associations that endpoint accepts. The relationship type, direction and
  endpoint labels come from the endpoint, never from the caller.
- ``sync.endpoints[].refnamespace`` on a node endpoint maps a provider record id
  to the graph ref ``<namespace>:<record id>``.

EIE reads its lockstep mirror of that spec and refuses, all-or-nothing, anything
CEG would refuse at load: a malformed entry anywhere, an ambiguous or conflicting
namespace, an association declared twice, a reversed or undeclared edge. Nothing
is guessed or defaulted, and there is no second mapping source.

The mirror is NOT CEG's approval. A resolution is labelled
``MAPPING_UNVERIFIED_MIRROR`` and ``mirror_drift`` is the explicit comparison
against an authoritative CEG spec. CEG's D-9 cross-check (Cognitive.Engine.Graphs
#422, closing #386) verifies an observed row only when the row carries the
association (``Settings.observed_association_send_enabled``, default off): CEG
refuses an association its own ``observedfrom`` does not declare, reported as
``MAPPING_REJECTED_BY_CEG``, and the producer labels a row CEG acknowledged with
the association on it ``MAPPING_VERIFIED_BY_CEG``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.engines.packet_router import RelationshipSyncContract, resolve_relationship_sync_contract

# The grammars CEG enforces (engine/config/schema.py); kept identical here.
PROVIDER_TOKEN = re.compile(r"[a-z][a-z0-9_-]{1,31}")
PROVIDER_OBJECT_TYPE = re.compile(r"[a-z][a-z0-9_]{0,63}|\d+-\d+")
PROVIDER_ASSOCIATION_CATEGORY = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
REF_NAMESPACE = re.compile(r"[a-z0-9_.-]{1,64}")

_ENTRY_KEYS = frozenset(
    {"provider", "fromobjecttype", "toobjecttype", "associationcategory", "associationtypeid"}
)

# The mapping came from EIE's mirror and CEG did not cross-check it: the row
# carried no association, or CEG did not acknowledge the row.
MAPPING_UNVERIFIED_MIRROR = "eie_mirror_unverified_by_ceg"
# CEG acknowledged a row that carried the association, so CEG's own tenant spec
# declares it in ``observedfrom`` (D-9, observed_association_defects). It says
# nothing about the relationship's relevance, which CEG's spec owns.
MAPPING_VERIFIED_BY_CEG = "ceg_verified_observedfrom"

AssociationKey = tuple[str, str, str, str, int]


class ResolutionFailure(StrEnum):
    MALFORMED_DECLARATION = "malformed_declaration"
    UNDECLARED = "undeclared"
    AMBIGUOUS = "ambiguous"
    INVALID_ENDPOINT = "invalid_endpoint"
    NAMESPACE_UNDECLARED = "namespace_undeclared"


@dataclass(frozen=True, slots=True)
class ObservedRelationshipContract:
    """The one tenant relationship an observed provider association stands for."""

    sync: RelationshipSyncContract
    provider: str
    from_object_type: str
    to_object_type: str
    association_category: str
    association_type_id: int
    subject_ref_namespace: str
    object_ref_namespace: str
    # Digest of the mirror's mapping declarations this contract was read from.
    mapping_fingerprint: str
    # Who approved the mapping when it was resolved: EIE's mirror. Only a CEG
    # acknowledgment of a row carrying the association upgrades a producer
    # result to MAPPING_VERIFIED_BY_CEG (D-9).
    mapping_verification: str = MAPPING_UNVERIFIED_MIRROR


@dataclass(frozen=True, slots=True)
class ObservedAssociationResolution:
    contract: ObservedRelationshipContract | None
    failure: ResolutionFailure | None
    detail: str


def _endpoints(domain_spec: dict[str, Any]) -> list[Any] | None:
    sync = domain_spec.get("sync")
    if sync is None:
        return []
    endpoints = sync.get("endpoints") if isinstance(sync, dict) else None
    return endpoints if isinstance(endpoints, list) else None


def _matches(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _entry_key(entry: Any) -> AssociationKey | None:
    """An ``observedfrom`` entry as its association key, or None when CEG would refuse it."""
    if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
        return None
    type_id = entry["associationtypeid"]
    if isinstance(type_id, bool) or not isinstance(type_id, int) or type_id < 1:
        return None
    if not (
        _matches(PROVIDER_TOKEN, entry["provider"])
        and _matches(PROVIDER_OBJECT_TYPE, entry["fromobjecttype"])
        and _matches(PROVIDER_OBJECT_TYPE, entry["toobjecttype"])
        and _matches(PROVIDER_ASSOCIATION_CATEGORY, entry["associationcategory"])
    ):
        return None
    return (
        entry["provider"],
        entry["fromobjecttype"],
        entry["toobjecttype"],
        entry["associationcategory"],
        type_id,
    )


def declared_ref_namespaces(domain_spec: dict[str, Any]) -> dict[str, str] | None:
    """Label -> declared ref namespace, or None when the declarations are malformed or conflict.

    Mirrors CEG: node endpoints only, one namespace per label across all its
    endpoints (declared on every one or none), one label per namespace, and a
    namespaced label is never the target of a ``childsync`` / ``taxonomyedges``
    write.
    """
    endpoints = _endpoints(domain_spec)
    if endpoints is None:
        return None
    by_label: dict[str, set[str | None]] = {}
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        namespace = endpoint.get("refnamespace")
        label = endpoint.get("targetnode")
        if namespace is not None:
            if endpoint.get("targetedge") or not isinstance(label, str) or not label:
                return None
            if not _matches(REF_NAMESPACE, namespace):
                return None
        if isinstance(label, str) and label:
            by_label.setdefault(label, set()).add(namespace)
    resolved: dict[str, str] = {}
    for label, namespaces in by_label.items():
        declared = {ns for ns in namespaces if ns is not None}
        if not declared:
            continue
        if len(namespaces) > 1:
            return None
        resolved[label] = declared.pop()
    if len(set(resolved.values())) != len(resolved):
        return None
    for label in _indirectly_written_labels(endpoints):
        if label in resolved:
            return None
    return resolved


def _indirectly_written_labels(endpoints: list[Any]) -> set[str]:
    """Labels a ``childsync`` or ``taxonomyedges`` entry writes (CEG forbids namespacing them)."""
    labels: set[str] = set()
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        for key, field in (("childsync", "targetnode"), ("taxonomyedges", "targetlabel")):
            entries = endpoint.get(key)
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict) and isinstance(entry.get(field), str):
                    labels.add(entry[field])
    return labels


def _has_stable_id(domain_spec: dict[str, Any], label: str) -> bool:
    """CEG's id rule for a relationship endpoint label: one node-endpoint idproperty, else a required property."""
    declared = {
        endpoint.get("idproperty")
        for endpoint in _endpoints(domain_spec) or []
        if isinstance(endpoint, dict)
        and endpoint.get("targetnode") == label
        and endpoint.get("idproperty")
    }
    if len(declared) > 1:
        return False
    if declared:
        return True
    ontology = domain_spec.get("ontology")
    nodes = ontology.get("nodes") if isinstance(ontology, dict) else None
    for node in nodes if isinstance(nodes, list) else []:
        if isinstance(node, dict) and node.get("label") == label:
            properties = node.get("properties")
            return any(
                isinstance(prop, dict) and prop.get("required") is True
                for prop in (properties if isinstance(properties, list) else [])
            )
    return False


def _declared_associations(
    domain_spec: dict[str, Any],
) -> list[tuple[AssociationKey, dict[str, Any]]] | None:
    """Every declared association with its owning endpoint; None when any entry is malformed."""
    endpoints = _endpoints(domain_spec)
    if endpoints is None:
        return None
    declared: list[tuple[AssociationKey, dict[str, Any]]] = []
    for endpoint in endpoints:
        if not isinstance(endpoint, dict) or "observedfrom" not in endpoint:
            continue
        entries = endpoint["observedfrom"]
        if not isinstance(entries, list) or not entries:
            return None
        for entry in entries:
            key = _entry_key(entry)
            if key is None:
                return None
            declared.append((key, endpoint))
    return declared


def resolve_observed_association_contract(
    domain_spec: dict[str, Any],
    *,
    provider: str,
    from_object_type: str,
    to_object_type: str,
    association_category: str,
    association_type_id: int,
) -> ObservedAssociationResolution:
    """The tenant relationship this exact association is declared for, or why there is none.

    All-or-nothing: a malformed declaration, or any association declared more
    than once, anywhere in the spec refuses every association -- CEG would not
    load that spec at all. Direction is part of the key, so a reversed
    object-type pair is a different, undeclared association.
    """
    declared = _declared_associations(domain_spec)
    namespaces = declared_ref_namespaces(domain_spec)
    if declared is None or namespaces is None:
        return ObservedAssociationResolution(
            None, ResolutionFailure.MALFORMED_DECLARATION, "malformed observedfrom or refnamespace"
        )
    key: AssociationKey = (
        provider,
        from_object_type,
        to_object_type,
        association_category,
        association_type_id,
    )
    keys = [declared_key for declared_key, _ in declared]
    duplicated = {k for k in keys if keys.count(k) > 1}
    if key in duplicated:
        return ObservedAssociationResolution(
            None, ResolutionFailure.AMBIGUOUS, f"{key} declared {keys.count(key)} times"
        )
    if duplicated:
        return ObservedAssociationResolution(
            None,
            ResolutionFailure.MALFORMED_DECLARATION,
            f"associations declared more than once: {sorted(duplicated)}",
        )
    owners = [endpoint for declared_key, endpoint in declared if declared_key == key]
    if not owners:
        return ObservedAssociationResolution(
            None, ResolutionFailure.UNDECLARED, f"{key} not declared"
        )
    endpoint = owners[0]
    sync = resolve_relationship_sync_contract(
        domain_spec,
        relation_type=endpoint.get("targetedge"),  # type: ignore[arg-type]
        from_label=endpoint.get("fromnode"),  # type: ignore[arg-type]
        to_label=endpoint.get("tonode"),  # type: ignore[arg-type]
    )
    if sync is None or not all(
        _has_stable_id(domain_spec, label) for label in (sync.from_label, sync.to_label)
    ):
        return ObservedAssociationResolution(
            None,
            ResolutionFailure.INVALID_ENDPOINT,
            f"endpoint {endpoint.get('path')!r} is not one valid DIRECTED sync-managed relationship",
        )
    subject_namespace = namespaces.get(sync.from_label)
    object_namespace = namespaces.get(sync.to_label)
    if subject_namespace is None or object_namespace is None:
        return ObservedAssociationResolution(
            None,
            ResolutionFailure.NAMESPACE_UNDECLARED,
            f"{sync.from_label!r} or {sync.to_label!r} declares no refnamespace",
        )
    contract = ObservedRelationshipContract(
        sync=sync,
        provider=provider,
        from_object_type=from_object_type,
        to_object_type=to_object_type,
        association_category=association_category,
        association_type_id=association_type_id,
        subject_ref_namespace=subject_namespace,
        object_ref_namespace=object_namespace,
        mapping_fingerprint=mapping_fingerprint(domain_spec),
    )
    return ObservedAssociationResolution(contract, None, "declared")


def mapping_projection(domain_spec: dict[str, Any]) -> dict[str, Any]:
    """The part of a tenant spec that decides observed relationships, in canonical form.

    Every endpoint that can change what an observation resolves to: those that
    declare ``observedfrom`` or ``refnamespace``, every other endpoint of a label
    they involve (it decides namespace and id consistency), and any endpoint whose
    ``childsync`` / ``taxonomyedges`` write such a label -- with their structural
    keys -- plus the ontology edges the observed endpoints write. Relevance is
    CEG's and is not part of the mirror contract.
    """
    endpoints = [e for e in _endpoints(domain_spec) or [] if isinstance(e, dict)]
    labels = {
        label
        for e in endpoints
        if "observedfrom" in e or "refnamespace" in e
        for label in (e.get("targetnode"), e.get("fromnode"), e.get("tonode"))
        if isinstance(label, str)
    }
    relevant = [
        e
        for e in endpoints
        if "observedfrom" in e
        or "refnamespace" in e
        or e.get("targetnode") in labels
        or _indirectly_written_labels([e]) & labels
    ]
    structural = (
        "path",
        "targetnode",
        "targetedge",
        "fromnode",
        "tonode",
        "idproperty",
        "childsync",
        "taxonomyedges",
    )
    projected = []
    for endpoint in relevant:
        item = {k: endpoint.get(k) for k in structural}
        item["refnamespace"] = endpoint.get("refnamespace")
        entries = endpoint.get("observedfrom")
        item["observedfrom"] = (
            sorted((json.dumps(e, sort_keys=True) for e in entries), key=str)
            if isinstance(entries, list)
            else entries
        )
        projected.append(item)
    edge_signatures = {
        (e.get("targetedge"), e.get("fromnode"), e.get("tonode"))
        for e in relevant
        if "observedfrom" in e
    }
    ontology = domain_spec.get("ontology")
    edges = ontology.get("edges") if isinstance(ontology, dict) else None
    written = sorted(
        (
            {k: edge.get(k) for k in ("type", "from", "to", "direction", "managedby")}
            for edge in (edges if isinstance(edges, list) else [])
            if isinstance(edge, dict)
            and (edge.get("type"), edge.get("from"), edge.get("to")) in edge_signatures
        ),
        key=lambda e: json.dumps(e, sort_keys=True),
    )
    return {
        "endpoints": sorted(projected, key=lambda e: json.dumps(e, sort_keys=True)),
        "edges": written,
    }


def mapping_fingerprint(domain_spec: dict[str, Any]) -> str:
    """Stable digest of ``mapping_projection``; equal digests mean an identical mapping."""
    canonical = json.dumps(mapping_projection(domain_spec), sort_keys=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def mirror_drift(mirror_spec: dict[str, Any], authoritative_spec: dict[str, Any]) -> list[str]:
    """How EIE's mirror differs from CEG's authoritative spec on the mapping; empty when in lockstep."""
    mirror, authority = mapping_projection(mirror_spec), mapping_projection(authoritative_spec)
    drift: list[str] = []
    for section in ("endpoints", "edges"):
        mine = {json.dumps(item, sort_keys=True) for item in mirror[section]}
        theirs = {json.dumps(item, sort_keys=True) for item in authority[section]}
        drift += [f"{section}: only in EIE mirror: {item}" for item in sorted(mine - theirs)]
        drift += [f"{section}: only in CEG spec: {item}" for item in sorted(theirs - mine)]
    return drift
