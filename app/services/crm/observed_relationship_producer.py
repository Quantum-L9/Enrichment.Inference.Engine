"""
Observed CRM association -> graph relationship evidence (QM.POC0.EIE.OBSERVED_RELATIONSHIP_PRODUCER.003).

One explicit, operator-invoked producer: it reads ONE association that
HubSpot actually reports between two records, turns it into a
``RelationshipAssertion`` and proposes it through the existing
``PacketRouter.notify_relationship_sync`` (Gate ``sync`` -> CEG). It is not
wired into enrichment, schedules nothing and writes nothing to the CRM.

Tenant-declared mapping (QM.POC0.TENANT_ASSOCIATION_MAPPING.004). The operator
names only WHICH provider association to read (``ObservedAssociationKey``:
object-type pair, association category and typeId) and the two record ids. The
tenant's spec decides what that association means: the one relationship
endpoint whose ``observedfrom`` declares it gives the relation type, direction
and endpoint labels, and the node endpoints' ``refnamespace`` turn each record
id into the node's graph ref. The producer holds no relationship vocabulary and
the caller cannot supply one. The mapping is resolved BEFORE the provider is
read: an undeclared, ambiguous, malformed or reversed mapping costs no HubSpot
call and sends nothing.

Mirror, not approval. EIE resolves the mapping from its lockstep mirror of the
tenant spec. CEG does not yet cross-check an observed row against its own copy
(D-9, Cognitive.Engine.Graphs#386), so every result after resolution carries
``mapping_verification`` (``MAPPING_UNVERIFIED_MIRROR``) and the mirror's
``mapping_fingerprint``; ``mirror_drift`` compares the mirror with CEG's spec.

What "observed" means here. The association exists in the authenticated
source at read time: ``assertion_status="observed"``, ``polarity="positive"``
and ``confidence=1.0`` state certainty of that membership only. They are not
purchase intent or commercial relevance; relevance is CEG's tenant-approved
``evidencerelevance``. HubSpot reports no revision for an association, so
``source_revision`` is None.

Endpoint identity. The producer cannot read the graph; CEG refuses a row whose
endpoints do not already exist under the tenant with exactly those graph refs,
and that refusal is reported as ``NOT_ACKNOWLEDGED``. No node is created here.

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
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog

from app.engines.observed_association_contract import ObservedRelationshipContract
from app.engines.packet_router import (
    CEG_OBSERVED_ASSOCIATION_REJECTED,
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
    native_object_type,
)

logger = structlog.get_logger("observed_relationship_producer")

PROVIDER = "hubspot"
# Certainty that the association is present in the source -- never relevance.
OBSERVED_MEMBERSHIP_CONFIDENCE = 1.0


class ProducerStatus(StrEnum):
    SYNCED = "SYNCED"
    INVALID_INPUT = "INVALID_INPUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    SOURCE_NOT_AVAILABLE = "SOURCE_NOT_AVAILABLE"
    CONTRACT_NOT_DECLARED = "CONTRACT_NOT_DECLARED"
    TRANSPORT_UNAVAILABLE = "TRANSPORT_UNAVAILABLE"
    NOT_ACKNOWLEDGED = "NOT_ACKNOWLEDGED"
    # D-9: CEG refused the provider association this mirror mapped, because CEG's
    # own tenant spec does not declare it -- the mirror has drifted from CEG.
    MAPPING_REJECTED_BY_CEG = "MAPPING_REJECTED_BY_CEG"


@dataclass(frozen=True, slots=True)
class ObservedAssociationKey:
    """The provider association the operator asks about. It names no tenant edge.

    ``from_object_type`` records are read; ``to_object_type`` records are their
    associated targets. Object types may be provider-native names or EIE aliases
    (``company``); the tenant spec declares the provider-native form.
    """

    from_object_type: str
    to_object_type: str
    association_category: str
    association_type_id: int


@dataclass(frozen=True, slots=True)
class ObservedRelationshipResult:
    status: ProducerStatus
    detail: str
    evidence_ref: str | None = None
    source_ref: str | None = None
    ceg_ack: dict[str, Any] | None = None
    # Set once a mapping was resolved: which spec mirror decided it, and that CEG
    # has not cross-checked it (D-9).
    mapping_fingerprint: str | None = None
    mapping_verification: str | None = None


def observed_evidence_ref(
    tenant_id: str, contract: ObservedRelationshipContract, association: HubSpotAssociation
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
        "category": contract.association_category,
        "type_id": contract.association_type_id,
        "ref_namespaces": [contract.subject_ref_namespace, contract.object_ref_namespace],
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


def _resolved(
    contract: ObservedRelationshipContract,
    status: ProducerStatus,
    detail: str,
    evidence_ref: str | None = None,
    source_ref: str | None = None,
    ceg_ack: dict[str, Any] | None = None,
) -> ObservedRelationshipResult:
    """A result after resolution: it always names the mirror mapping that decided it."""
    return ObservedRelationshipResult(
        status,
        detail,
        evidence_ref,
        source_ref,
        ceg_ack,
        mapping_fingerprint=contract.mapping_fingerprint,
        mapping_verification=contract.mapping_verification,
    )


def _key_defects(key: ObservedAssociationKey) -> list[str]:
    defects = [
        f"{name} missing"
        for name in ("from_object_type", "to_object_type", "association_category")
        if not isinstance(getattr(key, name), str) or not getattr(key, name).strip()
    ]
    type_id = key.association_type_id
    if isinstance(type_id, bool) or not isinstance(type_id, int) or type_id < 1:
        defects.append("association_type_id must be a positive integer")
    return defects


def _matching_association(
    associations: list[HubSpotAssociation],
    contract: ObservedRelationshipContract,
    to_record_id: str,
) -> HubSpotAssociation | None:
    for association in associations:
        if (
            association.to_record_id == to_record_id
            and association.category == contract.association_category
            and association.type_id == contract.association_type_id
        ):
            return association
    return None


async def produce_observed_relationship(
    *,
    tenant_id: str,
    key: ObservedAssociationKey,
    from_record_id: str,
    to_record_id: str,
    hubspot: HubSpotClient,
    router: PacketRouter,
    correlation_id: str | None = None,
    send_observed_association: bool | None = None,
) -> ObservedRelationshipResult:
    """Read one observed association and propose it as graph evidence. Never raises for a refusal.

    ``send_observed_association`` (default: ``Settings.observed_association_send_enabled``)
    sends the association on the row so CEG cross-checks it against its own spec (D-9).
    """
    if send_observed_association is None:
        from app.core.config import get_settings

        send_observed_association = get_settings().observed_association_send_enabled
    defects = _key_defects(key)
    for name, value in (("from_record_id", from_record_id), ("to_record_id", to_record_id)):
        if not is_canonical_record_id(value):
            defects.append(f"{name} must be a canonical numeric HubSpot record id")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        defects.append("tenant_id missing")
    if defects:
        return ObservedRelationshipResult(ProducerStatus.INVALID_INPUT, "; ".join(defects))

    from_type = native_object_type(key.from_object_type)
    to_type = native_object_type(key.to_object_type)
    contract = router.observed_association_contract(
        tenant_id,
        provider=PROVIDER,
        from_object_type=from_type,
        to_object_type=to_type,
        association_category=key.association_category,
        association_type_id=key.association_type_id,
    )
    if contract is None:
        return ObservedRelationshipResult(
            ProducerStatus.CONTRACT_NOT_DECLARED,
            f"tenant {tenant_id!r} declares no relationship for the {PROVIDER} association "
            f"{from_type}->{to_type} {key.association_category}:{key.association_type_id} "
            "(one observedfrom entry on a valid relationship endpoint, refnamespace on both nodes)",
        )
    logger.info(
        "observed_association_mapping_resolved",
        tenant_id=tenant_id,
        relation_type=contract.sync.relation_type,
        entity_type=contract.sync.entity_type,
        mapping_fingerprint=contract.mapping_fingerprint,
        mapping_verification=contract.mapping_verification,
    )

    try:
        associations = await asyncio.to_thread(
            hubspot.read_associations, from_type, from_record_id, to_type
        )
    except HubSpotAssociationReadError as exc:
        logger.warning(
            "observed_relationship_provider_unavailable",
            tenant_id=tenant_id,
            status_code=exc.status_code,
            reason=exc.reason,
        )
        return _resolved(contract, ProducerStatus.PROVIDER_UNAVAILABLE, exc.reason)

    association = _matching_association(associations, contract, to_record_id)
    if association is None:
        return _resolved(
            contract,
            ProducerStatus.SOURCE_NOT_AVAILABLE,
            f"HubSpot reports no {contract.association_category}:{contract.association_type_id} "
            f"association from {from_type}/{from_record_id} to {to_type}/{to_record_id}",
        )

    evidence_ref = observed_evidence_ref(tenant_id, contract, association)
    source_ref = observed_source_ref(association)
    assertion = RelationshipAssertion(
        subject_label=contract.sync.from_label,
        subject_id=f"{contract.subject_ref_namespace}:{association.from_record_id}",
        relation_type=contract.sync.relation_type,
        object_label=contract.sync.to_label,
        object_id=f"{contract.object_ref_namespace}:{association.to_record_id}",
        evidence_ref=evidence_ref,
        source_ref=source_ref,
        confidence=OBSERVED_MEMBERSHIP_CONFIDENCE,
        assertion_status="observed",
        polarity="positive",
        observed_association=(
            (
                contract.provider,
                contract.from_object_type,
                contract.to_object_type,
                contract.association_category,
                contract.association_type_id,
            )
            if send_observed_association
            else None
        ),
    )
    assertion_defects = relationship_assertion_defects(assertion)
    if assertion_defects:
        return _resolved(
            contract,
            ProducerStatus.INVALID_INPUT,
            "; ".join(assertion_defects),
            evidence_ref,
            source_ref,
        )

    # notify_relationship_sync re-resolves the endpoint by labels from the same
    # spec; that second resolution must agree (defence in depth).
    sync = await router.notify_relationship_sync(tenant_id, assertion, correlation_id)
    if sync.outcome is RelationshipSyncOutcome.ACKNOWLEDGED and sync.ack is not None:
        return _resolved(
            contract,
            ProducerStatus.SYNCED,
            "acknowledged by CEG",
            evidence_ref,
            source_ref,
            sync.ack,
        )
    if sync.outcome is RelationshipSyncOutcome.TRANSPORT_UNAVAILABLE:
        return _resolved(
            contract,
            ProducerStatus.TRANSPORT_UNAVAILABLE,
            f"Gate could not carry the relationship after retries: {sync.detail}",
            evidence_ref,
            source_ref,
        )
    if (
        assertion.observed_association is not None
        and CEG_OBSERVED_ASSOCIATION_REJECTED in sync.detail
    ):
        logger.warning(
            "observed_association_mirror_drift",
            tenant_id=tenant_id,
            relation_type=contract.sync.relation_type,
            entity_type=contract.sync.entity_type,
            mapping_fingerprint=contract.mapping_fingerprint,
            detail=sync.detail,
        )
        return _resolved(
            contract,
            ProducerStatus.MAPPING_REJECTED_BY_CEG,
            "CEG refused the association this node's spec mirror mapped; the mirror has "
            f"drifted from CEG's tenant spec: {sync.detail}",
            evidence_ref,
            source_ref,
        )
    return _resolved(
        contract,
        ProducerStatus.NOT_ACKNOWLEDGED,
        f"CEG did not acknowledge the relationship: {sync.detail}",
        evidence_ref,
        source_ref,
    )
