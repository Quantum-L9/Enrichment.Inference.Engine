"""
Observed CRM association -> graph relationship evidence (QM.POC0.EIE.OBSERVED_RELATIONSHIP_PRODUCER.003).

One explicit, operator-invoked producer: it reads ONE association that
HubSpot actually reports between two records, turns it into a
``RelationshipAssertion`` and proposes it through the existing
``PacketRouter.notify_relationship_sync`` (Gate ``sync`` -> CEG). It is not
wired into enrichment, schedules nothing and writes nothing to the CRM.

Domain neutrality. The producer holds no relationship vocabulary. The operator
supplies an ``ObservedAssociationBinding`` per invocation naming which provider
association (object-type pair + association category/typeId) stands for which
tenant edge (subject label, relation type, object label). That edge must be
declared by the tenant's own spec (DIRECTED, sync-managed, one ``targetedge``
endpoint) -- the same declaration CEG validates -- or nothing is sent.

What "observed" means here. The association exists in the authenticated
source at read time: ``assertion_status="observed"``, ``polarity="positive"``
and ``confidence=1.0`` state certainty of that membership only. They are not
purchase intent or commercial relevance; relevance is CEG's server-side map.
HubSpot reports no revision for an association, so ``source_revision`` is None.

Endpoint identity. A graph node's stable id is a namespaced ref
``<namespace>:<record id>`` -- CEG scores only refs of that shape
(``engine/scoring/evidence_priority.py``: ``[a-z0-9_.-]+``, a colon, a non-blank id) -- so the binding
names the ref namespace of each endpoint and the producer emits
``<subject_ref_namespace>:<from record id>`` and
``<object_ref_namespace>:<to record id>``. Nothing defaults it. The producer
cannot read the graph; CEG refuses a row whose endpoints do not already exist
under the tenant with exactly that id, and that refusal is reported as
``NOT_ACKNOWLEDGED``. No node is created here.

Every outcome is a typed ``ObservedRelationshipResult``; only ``SYNCED`` means
CEG acknowledged the edge. The router's typed answer decides the rest on
transport signals only (GAR A5): Gate unreachable, timed out or 5xx after the
retries are spent is ``TRANSPORT_UNAVAILABLE`` (the same invocation may be
repeated; it reuses the evidence ref and Gate key); a worker ``failure``
packet, a Gate 4xx or an unconfirming answer is ``NOT_ACKNOWLEDGED``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog

from app.engines.packet_router import (
    PacketRouter,
    RelationshipAssertion,
    RelationshipSyncOutcome,
    relationship_assertion_defects,
)

from .hubspot_client import (
    HubSpotAssociation,
    HubSpotAssociationReadError,
    HubSpotClient,
    is_canonical_record_id,
)

logger = structlog.get_logger("observed_relationship_producer")

PROVIDER = "hubspot"
# Certainty that the association is present in the source -- never relevance.
OBSERVED_MEMBERSHIP_CONFIDENCE = 1.0
# The namespace half of CEG's entity ref grammar (``<namespace>:<id>``).
_REF_NAMESPACE = re.compile(r"[a-z0-9_.-]{1,64}")


class ProducerStatus(StrEnum):
    SYNCED = "SYNCED"
    INVALID_INPUT = "INVALID_INPUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    SOURCE_NOT_AVAILABLE = "SOURCE_NOT_AVAILABLE"
    CONTRACT_NOT_DECLARED = "CONTRACT_NOT_DECLARED"
    TRANSPORT_UNAVAILABLE = "TRANSPORT_UNAVAILABLE"
    NOT_ACKNOWLEDGED = "NOT_ACKNOWLEDGED"


@dataclass(frozen=True, slots=True)
class ObservedAssociationBinding:
    """Which provider association stands for which tenant-declared edge.

    Supplied explicitly by the operator for one invocation; there is no default.
    ``from_object_type`` records are the edge's subject, ``to_object_type``
    records its object; ``*_ref_namespace`` turns each record id into the
    endpoint node's stable id.
    """

    from_object_type: str
    to_object_type: str
    association_category: str
    association_type_id: int
    subject_label: str
    relation_type: str
    object_label: str
    subject_ref_namespace: str
    object_ref_namespace: str


@dataclass(frozen=True, slots=True)
class ObservedRelationshipResult:
    status: ProducerStatus
    detail: str
    evidence_ref: str | None = None
    source_ref: str | None = None
    ceg_ack: dict[str, Any] | None = None


def observed_evidence_ref(
    tenant_id: str, binding: ObservedAssociationBinding, association: HubSpotAssociation
) -> str:
    """Deterministic identity of one observed association for one tenant.

    Hashes tenant, provider, the ordered (from, to) object types and record ids,
    the association category/typeId and the endpoint ref namespaces. No
    timestamp, randomness or label: the same association always yields the
    same ref; a different direction, type or endpoint identity a different one.
    """
    identity = {
        "tenant": tenant_id,
        "provider": PROVIDER,
        "from": [association.from_object_type, association.from_record_id],
        "to": [association.to_object_type, association.to_record_id],
        "category": binding.association_category,
        "type_id": binding.association_type_id,
        "ref_namespaces": [binding.subject_ref_namespace, binding.object_ref_namespace],
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
    return f"evidence:{PROVIDER}:association:{digest[:24]}"


def observed_source_ref(association: HubSpotAssociation) -> str:
    """The provider association the evidence was read from (record ids only, no PII)."""
    return (
        f"{PROVIDER}:crm/associations/"
        f"{association.from_object_type}/{association.from_record_id}->"
        f"{association.to_object_type}/{association.to_record_id}"
        f"#{association.category}:{association.type_id}"
    )


def _binding_defects(binding: ObservedAssociationBinding) -> list[str]:
    defects = [
        f"{name} missing"
        for name in (
            "from_object_type",
            "to_object_type",
            "association_category",
            "subject_label",
            "relation_type",
            "object_label",
        )
        if not isinstance(getattr(binding, name), str) or not getattr(binding, name).strip()
    ]
    type_id = binding.association_type_id
    if isinstance(type_id, bool) or not isinstance(type_id, int):
        defects.append("association_type_id must be an integer")
    for name in ("subject_ref_namespace", "object_ref_namespace"):
        value = getattr(binding, name)
        if not isinstance(value, str) or not _REF_NAMESPACE.fullmatch(value):
            defects.append(f"{name} must match [a-z0-9_.-]{{1,64}}")
    return defects


def _matching_association(
    associations: list[HubSpotAssociation],
    binding: ObservedAssociationBinding,
    to_record_id: str,
) -> HubSpotAssociation | None:
    for association in associations:
        if (
            association.to_record_id == to_record_id
            and association.category == binding.association_category
            and association.type_id == binding.association_type_id
        ):
            return association
    return None


async def produce_observed_relationship(
    *,
    tenant_id: str,
    binding: ObservedAssociationBinding,
    from_record_id: str,
    to_record_id: str,
    hubspot: HubSpotClient,
    router: PacketRouter,
    correlation_id: str | None = None,
) -> ObservedRelationshipResult:
    """Read one observed association and propose it as graph evidence. Never raises for a refusal."""
    defects = _binding_defects(binding)
    for name, value in (("from_record_id", from_record_id), ("to_record_id", to_record_id)):
        if not is_canonical_record_id(value):
            defects.append(f"{name} must be a canonical numeric HubSpot record id")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        defects.append("tenant_id missing")
    if defects:
        return ObservedRelationshipResult(ProducerStatus.INVALID_INPUT, "; ".join(defects))

    try:
        associations = await asyncio.to_thread(
            hubspot.read_associations,
            binding.from_object_type,
            from_record_id,
            binding.to_object_type,
        )
    except HubSpotAssociationReadError as exc:
        logger.warning(
            "observed_relationship_provider_unavailable",
            tenant_id=tenant_id,
            status_code=exc.status_code,
            reason=exc.reason,
        )
        return ObservedRelationshipResult(ProducerStatus.PROVIDER_UNAVAILABLE, exc.reason)

    association = _matching_association(associations, binding, to_record_id)
    if association is None:
        return ObservedRelationshipResult(
            ProducerStatus.SOURCE_NOT_AVAILABLE,
            f"HubSpot reports no {binding.association_category}:{binding.association_type_id} "
            f"association from {binding.from_object_type}/{from_record_id} "
            f"to {binding.to_object_type}/{to_record_id}",
        )

    evidence_ref = observed_evidence_ref(tenant_id, binding, association)
    source_ref = observed_source_ref(association)
    assertion = RelationshipAssertion(
        subject_label=binding.subject_label,
        subject_id=f"{binding.subject_ref_namespace}:{association.from_record_id}",
        relation_type=binding.relation_type,
        object_label=binding.object_label,
        object_id=f"{binding.object_ref_namespace}:{association.to_record_id}",
        evidence_ref=evidence_ref,
        source_ref=source_ref,
        confidence=OBSERVED_MEMBERSHIP_CONFIDENCE,
        assertion_status="observed",
        polarity="positive",
    )
    assertion_defects = relationship_assertion_defects(assertion)
    if assertion_defects:
        return ObservedRelationshipResult(
            ProducerStatus.INVALID_INPUT, "; ".join(assertion_defects), evidence_ref, source_ref
        )
    if router.relationship_sync_contract(tenant_id, assertion) is None:
        return ObservedRelationshipResult(
            ProducerStatus.CONTRACT_NOT_DECLARED,
            f"tenant {tenant_id!r} declares no DIRECTED sync-managed "
            f"({binding.subject_label})-[:{binding.relation_type}]->({binding.object_label}) "
            "edge with one targetedge sync endpoint",
            evidence_ref,
            source_ref,
        )

    sync = await router.notify_relationship_sync(tenant_id, assertion, correlation_id)
    if sync.outcome is RelationshipSyncOutcome.ACKNOWLEDGED and sync.ack is not None:
        return ObservedRelationshipResult(
            ProducerStatus.SYNCED, "acknowledged by CEG", evidence_ref, source_ref, sync.ack
        )
    if sync.outcome is RelationshipSyncOutcome.TRANSPORT_UNAVAILABLE:
        return ObservedRelationshipResult(
            ProducerStatus.TRANSPORT_UNAVAILABLE,
            f"Gate could not carry the relationship after retries: {sync.detail}",
            evidence_ref,
            source_ref,
        )
    return ObservedRelationshipResult(
        ProducerStatus.NOT_ACKNOWLEDGED,
        f"CEG did not acknowledge the relationship: {sync.detail}",
        evidence_ref,
        source_ref,
    )
