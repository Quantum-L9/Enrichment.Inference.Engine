"""
app/engines/packet_router.py

Gate-routed transport router for inter-node constellation communication.

Every packet leaves through the one EIE Gate client
(``app.services.gate_client``) and is addressed to Gate; Gate resolves the
destination by action. EIE never names a peer.

Seam contract (seam audit 2026-09-02):

* ``notify_graph_sync`` speaks Cognitive.Engine.Graphs' executable ``sync``
  contract -- ``{"entity_type": <sync endpoint>, "batch": [row, ...]}`` where
  each row carries the endpoint's id property -- instead of the retired
  ``graph-sync`` dialect that no node owned and no Gate route resolved.
  ``score-invalidate`` is not dispatched: Gate does not own that action
  (non-canonical; no node is registered), so there is no acknowledgment to
  record and this router does not send it.
* Typed rows (owner decision 2026-11-11): the row schema an endpoint accepts is
  the tenant spec's declared types -- the target node's properties that are not
  ``evidenceonly``, its childsync fields included (``app.engines.spec_types``).
  :func:`project_graph_sync_row` builds every row from that declaration and
  encodes each value by its declared type (a ``list<map>`` is a list of maps,
  never a JSON string); a value that does not match its declaration refuses the
  row before anything is sent; an undeclared field is dropped and counted. CEG
  validates the same rows against its own tenant spec at ingress.
* GRAPH-IDENT-02: the endpoint and its id property are read per tenant from
  the tenant's own spec (tenant id == domain spec id: ``domains/<tenant>/spec.yaml``
  ``sync.endpoints[]``, CEG's owner-native ``path`` / ``targetnode`` /
  ``idproperty`` keys) -- the spec CEG loads to validate the same sync --
  never from a process-global constant or the payload's ``domain``. A tenant
  whose spec declares no resolvable endpoint falls back to the clearly labeled
  ``Settings.graph_sync_*`` pair when it is set (empty by default), and is
  otherwise skipped (fail closed: nothing is sent, a warning names it).
* Retry ownership for EIE-originated side effects lives HERE, once. Gate
  attempts every seam action exactly once (Gate replay_safety), so a retry
  below is the only retry. A retry is only taken when it cannot multiply a
  domain effect: Gate unreachable (nothing executed), or a timeout / 5xx on a
  packet that carries a stable idempotency key (Gate answers the duplicate from
  its keyed cache once the first attempt completed).
* One deadline per attempt; an operation is at most ``_MAX_ATTEMPTS`` attempts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog
import yaml
from constellation_node_sdk.gate import (
    GateClientError,
    GateConnectionError,
    GateHTTPError,
    GateTimeoutError,
)

from app.engines.domain_yaml_reader import DomainPublicationError, DomainYamlReader
from app.engines.spec_types import (
    TypeDefect,
    encode_value,
    row_properties,
    value_defects,
)

if TYPE_CHECKING:
    from app.engines.observed_association_contract import ObservedRelationshipContract
from app.services.gate_client import build_gate_client

logger = structlog.get_logger("packet_router")

# Per-attempt operation budget. Graph sync runs inside EIE's own inbound budget
# when triggered by a Gate-dispatched `enrich`, so it must stay well under the
# node cap EIE advertises (25 s): two attempts plus backoff fit inside it.
_ROUTE_TIMEOUT = 10.0
_MAX_ATTEMPTS = 2
_BACKOFF_BASE_SECONDS = 1.0

# Where a resolved graph-sync contract came from. Logged on every resolution so
# a fallback never passes for a spec-declared endpoint.
GRAPH_SYNC_SOURCE_DOMAIN_SPEC = "domain_spec"
GRAPH_SYNC_SOURCE_SETTINGS_FALLBACK = "settings_fallback"
_NO_CONTRACT_FIX = (
    "declare sync.endpoints[] in the domain spec, or set GRAPH_SYNC_ENTITY_TYPE "
    "and GRAPH_SYNC_ID_PROPERTY as the labeled fallback"
)


@dataclass(frozen=True, slots=True)
class GraphSyncContract:
    """The CEG ``sync`` endpoint one domain's enriched rows are addressed to.

    ``entity_type`` is the endpoint's path suffix (what CEG's sync handler keys
    on) and ``id_property`` the row key that endpoint merges on. ``row_schema``
    is the endpoint's typed row contract read from the tenant spec: the target
    node's declarations that ride a sync row (``spec_types.row_properties``).
    None means the spec declares no row for this endpoint -- the settings
    fallback, or a target node the spec's ontology does not declare -- and no
    row can be sent to it.
    """

    entity_type: str
    id_property: str
    source: str
    row_schema: dict[str, dict[str, Any]] | None = field(default=None, compare=False, repr=False)
    # The ontology node the endpoint writes (``targetnode``); None for the
    # settings fallback. Part of the pinned contract digest (A2).
    target_node: str | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class GraphSyncRowProjection:
    """One enriched entity projected onto its endpoint's typed row contract.

    ``row`` is None when the row is refused; ``defects`` then names every field,
    its declared type and the actual type. ``dropped`` lists the fields that are
    not declared for the row (never sent).
    """

    row: dict[str, Any] | None
    defects: tuple[TypeDefect, ...] = ()
    dropped: tuple[str, ...] = ()


def _ontology_labels(domain_spec: dict[str, Any]) -> set[str]:
    """Node labels the domain's own ontology declares, in either spec shape."""
    ontology = domain_spec.get("ontology")
    if not isinstance(ontology, dict):
        return set()
    nodes = ontology.get("nodes", ontology.get("entities", []))
    if isinstance(nodes, dict):
        return {str(k) for k in nodes}
    if isinstance(nodes, list):
        return {
            str(n["label"])
            for n in nodes
            if isinstance(n, dict) and isinstance(n.get("label"), str)
        }
    return set()


def _usable_sync_endpoints(domain_spec: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """``(entity_type, id_property, targetnode)`` for each endpoint that has both keys."""
    sync = domain_spec.get("sync")
    endpoints = sync.get("endpoints") if isinstance(sync, dict) else None
    if not isinstance(endpoints, list):
        return []
    usable: list[tuple[str, str, str | None]] = []
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        path = endpoint.get("path")
        id_property = endpoint.get("idproperty")
        if not isinstance(path, str) or not isinstance(id_property, str):
            continue
        entity_type = path.rstrip("/").rsplit("/", 1)[-1]
        if not entity_type or not id_property:
            continue
        target = endpoint.get("targetnode")
        usable.append((entity_type, id_property, target if isinstance(target, str) else None))
    return usable


def resolve_graph_sync_contract(
    domain_spec: dict[str, Any],
    *,
    node_label: str | None = None,
) -> GraphSyncContract | None:
    """Pick the CEG sync endpoint a domain's enriched entity rows belong to.

    Reads ``sync.endpoints[]`` exactly as CEG's ``SyncEndpointSpec`` declares it
    (``path``, ``targetnode``, ``idproperty``); CEG resolves ``entity_type`` as
    the last path segment, so that is what is returned. Selection, in order:

    1. ``node_label`` given and exactly one endpoint's ``targetnode`` is that
       label -> that endpoint;
    2. exactly one usable endpoint -> that endpoint (a single declared
       endpoint is the domain's sync target whatever the caller's label is:
       an `object_type` such as ``Account`` is a CRM object, not a node label);
    3. NO ``node_label`` given and exactly one endpoint whose ``targetnode``
       is a label of this domain's own ontology -> that endpoint.

    Anything else -- no ``sync`` section, no endpoint with both a path and an
    id property, several candidates none of which the label or the ontology
    singles out, or an explicit ``node_label`` that no endpoint among several
    targets -- is ``None``. Rule 3 does not override a label the caller
    supplied: that label names an entity type the spec does not declare, and
    the ontology's endpoint belongs to another one. The caller distinguishes
    "declares none" from "declares several, ambiguous" with
    :func:`_usable_sync_endpoints`; this function never guesses.
    """
    usable = _usable_sync_endpoints(domain_spec)
    if not usable:
        return None
    chosen = [c for c in usable if node_label and c[2] == node_label]
    if len(chosen) != 1:
        if len(usable) == 1:
            chosen = usable
        elif not node_label:
            labels = _ontology_labels(domain_spec)
            chosen = [c for c in usable if c[2] in labels]
    if len(chosen) != 1:
        return None
    entity_type, id_property, target = chosen[0]
    return GraphSyncContract(
        entity_type=entity_type,
        id_property=id_property,
        source=GRAPH_SYNC_SOURCE_DOMAIN_SPEC,
        row_schema=row_properties(domain_spec, target) if target else None,
        target_node=target,
    )


class NodeTarget(StrEnum):
    """Logical target names kept for call-site readability and logs only.

    Routing authority is Gate's: the *action* selects the destination. A target
    here never becomes a URL.
    """

    GRAPH = "graph"
    SCORE = "score"
    ROUTE = "route"
    SIGNAL = "signal"
    FORECAST = "forecast"
    HANDOFF = "handoff"


class NodeUnreachableError(Exception):
    """Raised when the Gate-routed operation could not be completed."""


class NodeOperationFailedError(NodeUnreachableError):
    """Gate completed the round trip, but the owning node answered a ``failure`` packet.

    The SDK runtime turns a handler exception into a ``failure`` packet (Gate_SDK
    ``create_error_transport_packet``) and Gate relays it with HTTP 200, so the
    client never sees a transport error. Gate itself refuses to cache such an
    answer (``_is_terminal_success``, SEAM-013); EIE applies the same reading.
    Subclasses ``NodeUnreachableError`` so every existing "could not be
    completed" handler keeps working.
    """


class GraphSyncOutcomeKind(StrEnum):
    """How one entity ``sync`` attempt ended, for the caller's retry decision.

    * ``ACKNOWLEDGED`` -- CEG confirmed this row at this endpoint.
    * ``RETRYABLE`` -- not confirmed, and the same pinned input may be sent
      again: Gate unavailable, a timeout or 5xx, a worker ``failure`` packet,
      an answer that does not confirm this row (wrong count or endpoint,
      malformed). The graph may or may not hold the row; a replay of
      the same row is a ``MERGE`` on the same id with the same idempotency key.
    * ``REFUSED`` -- the same input can never succeed: no endpoint resolves,
      the endpoint's row contract changed since the input was pinned, a value
      does not match its declared type, CEG rejected the batch in full, or Gate
      refused the packet (4xx). Terminal: sending the same input again cannot succeed.
    """

    ACKNOWLEDGED = "acknowledged"
    RETRYABLE = "retryable"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class GraphSyncOutcome:
    """Typed outcome of one ``notify_graph_sync`` attempt; ``ack`` only when acknowledged."""

    kind: GraphSyncOutcomeKind
    reason: str
    ack: dict[str, Any] | None = None
    retried: bool = False
    """True when the transport retried within this attempt: an earlier request may still land."""


@dataclass(frozen=True, slots=True)
class GraphSyncPin:
    """The endpoint a graph intent is pinned to when its result commits."""

    entity_type: str
    contract_digest: str
    cartridge_revision: str | None = None


def graph_sync_contract_digest(contract: GraphSyncContract) -> str:
    """Digest of an endpoint contract: entity type, id property, target node, row schema."""
    material = json.dumps(
        {
            "entity_type": contract.entity_type,
            "id_property": contract.id_property,
            "target_node": contract.target_node,
            "row_schema": contract.row_schema,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _rejected_in_full(ack: Any, entity_type: str) -> bool:
    """CEG refused this batch at this endpoint and wrote none of it.

    Only then is the same row refused again. A rejection naming another
    endpoint, or any count other than an integer 0, is not this request's
    answer and stays retryable.
    """
    synced = ack.get("synced_count") if isinstance(ack, dict) else None
    return (
        isinstance(ack, dict)
        and ack.get("status") == "all_rejected"
        and ack.get("entity_type") == entity_type
        and isinstance(synced, int)
        and not isinstance(synced, bool)
        and synced == 0
    )


def _unconfirmed_outcome(ack: Any, entity_type: str) -> tuple[GraphSyncOutcomeKind, str]:
    """Classify an answer that did not acknowledge the row: refused or retryable."""
    if _rejected_in_full(ack, entity_type):
        return GraphSyncOutcomeKind.REFUSED, "all_rejected"
    if _rows_rejected(ack, entity_type):
        return GraphSyncOutcomeKind.REFUSED, "rows_rejected"
    return GraphSyncOutcomeKind.RETRYABLE, "unacknowledged"


def graph_sync_failure_kind(exc: NodeUnreachableError) -> GraphSyncOutcomeKind:
    """Classify a failed entity ``sync`` route on Gate-client transport signals only.

    Unreachable, timed out, 5xx, a worker ``failure`` packet: retryable (bounded by the caller). A Gate 4xx, or any other client
    error: refused -- the same packet would be refused the same way. The
    worker's own error vocabulary is never read (GAR A5).
    """
    if isinstance(exc, NodeOperationFailedError):
        return GraphSyncOutcomeKind.RETRYABLE
    cause = exc.__cause__
    if isinstance(cause, GateConnectionError | GateTimeoutError):
        return GraphSyncOutcomeKind.RETRYABLE
    if isinstance(cause, GateHTTPError) and cause.is_server_error:
        return GraphSyncOutcomeKind.RETRYABLE
    return GraphSyncOutcomeKind.REFUSED


def _is_failure_packet(packet: Any) -> bool:
    """Gate's terminal-success predicate, read from transport-level signals only.

    A ``failure`` packet type, or a response hop whose status is ``failed``,
    means the operation did not complete — whatever the payload says.
    """
    if getattr(packet.header, "packet_type", None) == "failure":
        return True
    for hop in reversed(getattr(packet, "hop_trace", ()) or ()):
        if getattr(hop, "direction", None) == "response":
            return bool(getattr(hop, "status", None) == "failed")
    return False


def graph_sync_acknowledged(
    ack: Any,
    *,
    entity_type: str | None = None,
    expected_count: int | None = None,
) -> bool:
    """Whether ``ack`` is CEG's confirmation of the intended ``sync`` operation.

    Authoritative contract (Cognitive.Engine.Graphs ``engine/handlers.py``
    ``handle_sync``): success is ``{"status": "success", "entity_type": <endpoint>,
    "synced_count": len(batch)}``; a batch rejected in full answers
    ``{"status": "all_rejected", "synced_count": 0, ...}``; a handler exception
    never reaches the payload as a status — it becomes a ``failure`` packet.

    Anything else — ``None``, a non-mapping, a missing or non-success status, a
    non-positive ``synced_count``, a ``synced_count`` other than
    ``expected_count`` when the caller knows its batch size, a
    ``rejected_count`` that is present and not 0 (A1 matrix F-A1-01: a node ack
    carries no created/replayed counts, and a row CEG rejected is never read as
    synchronized), or an acknowledgment for a different endpoint — is NOT a
    synchronization. The graph may or may not hold the row; the caller must not
    record it as synced.
    """
    if not isinstance(ack, dict):
        return False
    if ack.get("status") != "success":
        return False
    synced = ack.get("synced_count")
    if isinstance(synced, bool) or not isinstance(synced, int) or synced < 1:
        return False
    if expected_count is not None and synced != expected_count:
        return False
    if "rejected_count" in ack and not _is_int(ack["rejected_count"], 0):
        return False
    return not (entity_type is not None and ack.get("entity_type") != entity_type)


def _is_int(value: Any, expected: int | None = None) -> bool:
    """An int (never a bool), equal to ``expected`` when one is given."""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return expected is None or value == expected


def _rows_rejected(ack: Any, entity_type: str) -> bool:
    """A success answer for this endpoint that names rows CEG rejected (F-A1-01).

    CEG validated those rows against its own tenant spec and refused them; the
    same pinned input is refused again, so it is terminal, never retried.
    """
    return (
        isinstance(ack, dict)
        and ack.get("status") == "success"
        and ack.get("entity_type") == entity_type
        and _is_int(ack.get("rejected_count"))
        and ack["rejected_count"] > 0
    )


def _retry_allowed(exc: BaseException, *, has_idempotency_key: bool) -> bool:
    """Whether a second attempt cannot duplicate a domain effect.

    * Gate unreachable: nothing was executed anywhere -> safe.
    * Timeout or Gate 5xx: the worker may have executed -> safe only under a
      stable idempotency key, which Gate answers from its keyed cache.
    * Any 4xx (unknown route, rejected packet, policy): never -- the same packet
      would be rejected the same way.
    """
    if isinstance(exc, GateConnectionError):
        return True
    if isinstance(exc, GateTimeoutError):
        return has_idempotency_key
    if isinstance(exc, GateHTTPError):
        return exc.is_server_error and has_idempotency_key
    return False


SYNC_VERSION_PROPERTY = "sync_version"


def _with_sync_version(
    fields: dict[str, Any], version: int | None, contract: GraphSyncContract
) -> dict[str, Any]:
    """``fields`` with the transport version set when the endpoint declares it (rule 6).

    A value of that name in ``fields`` is never sent as data: it is replaced by
    the durable version, or dropped when there is none or none is declared.
    """
    row = {k: v for k, v in fields.items() if k != SYNC_VERSION_PROPERTY}
    declared = contract.row_schema is not None and SYNC_VERSION_PROPERTY in contract.row_schema
    if declared and version is not None:
        row[SYNC_VERSION_PROPERTY] = version
    return row


def project_graph_sync_row(
    *,
    entity_id: str,
    fields: dict[str, Any],
    contract: GraphSyncContract,
) -> GraphSyncRowProjection:
    """Project enriched fields onto one CEG sync row, by the endpoint's declared types.

    The row is the endpoint's id property (the entity id) plus every declared
    row property present in ``fields``, each encoded by its declaration
    (``spec_types.encode_value``): a ``list<map>`` is a list of mappings that
    keep only their declared keys, never a JSON string. Fails closed:

    * a contract with no ``row_schema`` refuses every row (nothing typed can be
      built for an endpoint the spec does not declare);
    * any value that does not match its declaration -- a JSON string or a list
      of strings for a ``list<map>``, ``"high"`` for a ``float`` -- refuses the
      whole row, with one defect per field naming the declared and actual type.

    A field the declaration does not name is dropped and listed in ``dropped``;
    so are EIE's own ``evidenceonly`` properties. A ``None`` value of a
    non-required property is sent as a clear (CEG applies ``SET n += row``).
    Presence of other required properties is not enforced here: a row is a
    partial ``MERGE`` update and CEG's ingress validation owns presence.
    """
    schema = contract.row_schema
    if schema is None:
        return GraphSyncRowProjection(
            row=None,
            defects=(
                TypeDefect(
                    contract.id_property,
                    "declared row schema",
                    "none",
                    f"endpoint {contract.entity_type!r} has no row declared in the tenant spec",
                ),
            ),
        )
    defects: list[TypeDefect] = []
    id_decl = schema.get(contract.id_property, {"type": "string"})
    defects.extend(value_defects(entity_id, id_decl, contract.id_property))
    row: dict[str, Any] = {contract.id_property: entity_id}
    dropped: list[str] = []
    for key, value in fields.items():
        if key == contract.id_property:
            continue
        decl = schema.get(key)
        if decl is None:
            dropped.append(key)
            continue
        found = value_defects(value, decl, key)
        if found:
            defects.extend(found)
            continue
        row[key] = encode_value(value, decl)
    if defects:
        return GraphSyncRowProjection(row=None, defects=tuple(defects), dropped=tuple(dropped))
    row[contract.id_property] = encode_value(entity_id, id_decl)
    return GraphSyncRowProjection(row=row, dropped=tuple(dropped))


def _sync_idempotency_key(
    tenant_id: str, entity_id: str, row: dict[str, Any], operation_key: str | None = None
) -> str:
    """Gate key of one sync send.

    With ``operation_key`` (the durable operation identity, A2) the key binds
    that operation as well as the row: retries of one committed version reuse
    it, while two versions that happen to produce the same row -- 1 -> 2 -> 1 --
    never share it, so Gate's cache cannot answer the third with the first's
    acknowledgment while the graph still holds 2.
    """
    material = json.dumps(row, sort_keys=True, default=str)
    if operation_key is not None:
        material = f"{operation_key}\n{material}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"eie:sync:{tenant_id}:{entity_id}:{digest}"


# ── Relationship evidence sync (QM.POC0.EIE.GRAPH_EVIDENCE.002B) ───────────────
#
# EIE proposes one sourced, typed relationship; CEG validates and materialises
# it through the same Gate ``sync`` action, at an endpoint the tenant's spec
# declares with ``targetedge`` / ``fromnode`` / ``tonode`` (CEG
# QM.POC0.CEG.GRAPH_EVIDENCE.002A). Nothing here infers a relationship: the
# caller hands over an explicit assertion, and an incomplete one is skipped
# with its reasons, never repaired. Relevance and admission are CEG's.

RELATIONSHIP_ASSERTION_STATUSES = frozenset({"observed", "reported", "inferred"})
RELATIONSHIP_POLARITIES = frozenset({"positive", "negative"})
# CEG's row key for D-9 provenance (Cognitive.Engine.Graphs engine/sync/generator.py).
OBSERVED_ASSOCIATION_KEY = "observed_association"
# The error class CEG answers when its own tenant spec does not declare the
# association a row claims (engine/handlers.py). A failure packet carries the
# class name, not the message.
CEG_OBSERVED_ASSOCIATION_REJECTED = "ObservedAssociationRejectedError"
OBSERVED_ASSOCIATION_FIELDS = (
    "provider",
    "fromobjecttype",
    "toobjecttype",
    "associationcategory",
    "associationtypeid",
)


@dataclass(frozen=True, slots=True)
class RelationshipAssertion:
    """One directed, typed relationship and the evidence for it.

    ``subject_*`` is the relationship's source node, ``object_*`` its target;
    the ids are the nodes' declared stable ids in the tenant graph. Every
    evidence field is supplied by the producer -- confidence, status and
    polarity are never defaulted.
    """

    subject_label: str
    subject_id: str | int
    relation_type: str
    object_label: str
    object_id: str | int
    evidence_ref: str
    source_ref: str
    confidence: float
    assertion_status: str
    polarity: str
    source_revision: str | None = None
    expires_at: str | None = None
    # D-9: the provider association an observed row was read from, as
    # (provider, fromobjecttype, toobjecttype, associationcategory,
    # associationtypeid). CEG checks it against its own ``observedfrom``.
    observed_association: tuple[str, str, str, str, int] | None = None


@dataclass(frozen=True, slots=True)
class RelationshipSyncContract:
    """The CEG ``sync`` endpoint that materialises one declared relationship."""

    entity_type: str
    relation_type: str
    from_label: str
    to_label: str


def _non_empty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def relationship_assertion_defects(assertion: RelationshipAssertion) -> list[str]:
    """Why ``assertion`` cannot be proposed as evidence; empty when it can."""
    defects: list[str] = []
    for name in ("subject_label", "relation_type", "object_label", "evidence_ref", "source_ref"):
        if not _non_empty_text(getattr(assertion, name)):
            defects.append(f"{name} missing")
    for name in ("subject_id", "object_id"):
        value = getattr(assertion, name)
        if isinstance(value, bool) or not (isinstance(value, int) or _non_empty_text(value)):
            defects.append(f"{name} missing")
    confidence = assertion.confidence
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not 0.0 <= confidence <= 1.0
    ):
        defects.append("confidence must be an explicit number in [0, 1]")
    if assertion.assertion_status not in RELATIONSHIP_ASSERTION_STATUSES:
        defects.append(f"assertion_status must be one of {sorted(RELATIONSHIP_ASSERTION_STATUSES)}")
    if assertion.polarity not in RELATIONSHIP_POLARITIES:
        defects.append(f"polarity must be one of {sorted(RELATIONSHIP_POLARITIES)}")
    for name in ("source_revision", "expires_at"):
        value = getattr(assertion, name)
        if value is not None and not _non_empty_text(value):
            defects.append(f"{name} must be a non-empty string when present")
    association = assertion.observed_association
    if association is not None:
        if assertion.assertion_status != "observed":
            defects.append("observed_association is sent only with assertion_status observed")
        if not _is_association_key(association):
            defects.append(
                "observed_association must be (provider, fromobjecttype, toobjecttype, "
                "associationcategory, associationtypeid)"
            )
    return defects


def _is_association_key(value: Any) -> bool:
    if not isinstance(value, tuple) or len(value) != len(OBSERVED_ASSOCIATION_FIELDS):
        return False
    *texts, type_id = value
    return (
        all(_non_empty_text(text) for text in texts)
        and isinstance(type_id, int)
        and not isinstance(type_id, bool)
        and type_id >= 1
    )


def _declares_sync_edge(domain_spec: dict[str, Any], contract: RelationshipSyncContract) -> bool:
    """Whether the ontology declares exactly this DIRECTED, sync-managed edge."""
    ontology = domain_spec.get("ontology")
    edges = ontology.get("edges") if isinstance(ontology, dict) else None
    if not isinstance(edges, list):
        return False
    matches = [
        edge
        for edge in edges
        if isinstance(edge, dict)
        and edge.get("type") == contract.relation_type
        and edge.get("from") == contract.from_label
        and edge.get("to") == contract.to_label
    ]
    return (
        len(matches) == 1
        and matches[0].get("direction") == "DIRECTED"
        and matches[0].get("managedby") == "sync"
    )


def resolve_relationship_sync_contract(
    domain_spec: dict[str, Any],
    *,
    relation_type: str,
    from_label: str,
    to_label: str,
) -> RelationshipSyncContract | None:
    """The tenant's declared endpoint for exactly this directed relationship, else None.

    Requires one ``sync.endpoints[]`` entry with this ``targetedge`` /
    ``fromnode`` / ``tonode`` and no ``targetnode``, and the matching
    DIRECTED, sync-managed edge in ``ontology.edges`` -- the same declaration
    CEG validates the row against. A reversed direction, an undeclared type,
    or two candidate endpoints resolve to None; nothing is guessed.
    """
    sync = domain_spec.get("sync")
    endpoints = sync.get("endpoints") if isinstance(sync, dict) else None
    if not isinstance(endpoints, list):
        return None
    candidates = [
        endpoint
        for endpoint in endpoints
        if isinstance(endpoint, dict)
        and endpoint.get("targetedge") == relation_type
        and endpoint.get("fromnode") == from_label
        and endpoint.get("tonode") == to_label
        and not endpoint.get("targetnode")
        and isinstance(endpoint.get("path"), str)
    ]
    if len(candidates) != 1:
        return None
    entity_type = candidates[0]["path"].rstrip("/").rsplit("/", 1)[-1]
    if not entity_type:
        return None
    contract = RelationshipSyncContract(
        entity_type=entity_type,
        relation_type=relation_type,
        from_label=from_label,
        to_label=to_label,
    )
    return contract if _declares_sync_edge(domain_spec, contract) else None


def _relationship_sync_row(assertion: RelationshipAssertion) -> dict[str, Any]:
    """CEG's relationship sync row: endpoint ids and evidence, nothing structural."""
    row: dict[str, Any] = {
        "from_id": assertion.subject_id,
        "to_id": assertion.object_id,
        "evidence_ref": assertion.evidence_ref,
        "source_ref": assertion.source_ref,
        "confidence": assertion.confidence,
        "assertion_status": assertion.assertion_status,
        "polarity": assertion.polarity,
    }
    if assertion.source_revision is not None:
        row["source_revision"] = assertion.source_revision
    if assertion.expires_at is not None:
        row["expires_at"] = assertion.expires_at
    if assertion.observed_association is not None:
        row[OBSERVED_ASSOCIATION_KEY] = dict(
            zip(OBSERVED_ASSOCIATION_FIELDS, assertion.observed_association, strict=True)
        )
    return row


def _relationship_idempotency_key(
    tenant_id: str, contract: RelationshipSyncContract, row: dict[str, Any]
) -> str:
    """Pinned to the exact assertion: a retry resends this assertion, never another.

    The row carries only endpoint ids and evidence; the structural identity
    (endpoint, relationship type, endpoint labels) comes from the resolved
    contract. All of it is hashed with the tenant, so one ``evidence_ref``
    proposed for a different relationship or endpoint is a different request
    and can never be answered from Gate's keyed cache for the first one.
    """
    identity = {
        "tenant": tenant_id,
        "entity_type": contract.entity_type,
        "relation_type": contract.relation_type,
        "from_label": contract.from_label,
        "to_label": contract.to_label,
        "row": row,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"eie:relsync:{tenant_id}:{row['evidence_ref']}:{digest}"


def relationship_sync_acknowledged(
    ack: Any,
    *,
    entity_type: str,
    expected_count: int,
) -> bool:
    """Whether ``ack`` is CEG's relationship endpoint confirming this batch.

    A node ``sync`` acknowledgment is not enough: CEG's relationship path also
    answers ``created_count`` + ``replayed_count`` == the rows it recorded, so an
    answer from a node endpoint can never mark a relationship synced.
    """
    if not graph_sync_acknowledged(ack, entity_type=entity_type, expected_count=expected_count):
        return False
    created = ack.get("created_count")
    replayed = ack.get("replayed_count")
    counts = (created, replayed)
    if any(isinstance(c, bool) or not isinstance(c, int) or c < 0 for c in counts):
        return False
    return bool(created + replayed == expected_count)


class RelationshipSyncOutcome(StrEnum):
    """What became of one proposed relationship, read from transport signals only.

    * ``ACKNOWLEDGED`` -- CEG's relationship endpoint confirmed this row.
    * ``NOT_PROPOSED`` -- nothing was sent: the assertion is incomplete or the
      tenant declares no endpoint for it.
    * ``TRANSPORT_UNAVAILABLE`` -- Gate could not carry the row: unreachable, a
      timeout, or a Gate 5xx, after the retries the pinned idempotency key
      allows were spent. The graph may or may not hold the row; a later retry of
      the same assertion reuses the same key.
    * ``NOT_ACKNOWLEDGED`` -- Gate carried it and the answer is not a
      confirmation: a ``failure`` packet, a Gate 4xx, an answer that does not
      confirm this row on this endpoint, or any other client failure.

    The worker's own error vocabulary (its exception name inside a ``failure``
    packet) is never read: telling a refusal apart from an outage by a peer's
    class names would couple EIE to that peer (GAR A5).
    """

    ACKNOWLEDGED = "acknowledged"
    NOT_PROPOSED = "not_proposed"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"
    NOT_ACKNOWLEDGED = "not_acknowledged"


@dataclass(frozen=True, slots=True)
class RelationshipSyncResult:
    """Typed answer of ``PacketRouter.notify_relationship_sync``; ``ack`` only when acknowledged."""

    outcome: RelationshipSyncOutcome
    detail: str
    ack: dict[str, Any] | None = None

    @property
    def acknowledged(self) -> bool:
        return self.outcome is RelationshipSyncOutcome.ACKNOWLEDGED


def relationship_failure_outcome(exc: NodeUnreachableError) -> RelationshipSyncOutcome:
    """Classify a failed relationship route on Gate-client transport signals only.

    ``route`` raises ``NodeOperationFailedError`` for a ``failure`` packet and
    ``NodeUnreachableError`` chained (``from``) to the last ``GateClientError``
    once its retries are spent. Unreachable, timed out and Gate 5xx mean the
    transport was unavailable; everything else -- a failure packet, a 4xx, any
    other client error -- is a refusal or an unconfirmed answer.
    """
    if isinstance(exc, NodeOperationFailedError):
        return RelationshipSyncOutcome.NOT_ACKNOWLEDGED
    cause = exc.__cause__
    if isinstance(cause, GateConnectionError | GateTimeoutError):
        return RelationshipSyncOutcome.TRANSPORT_UNAVAILABLE
    if isinstance(cause, GateHTTPError) and cause.is_server_error:
        return RelationshipSyncOutcome.TRANSPORT_UNAVAILABLE
    return RelationshipSyncOutcome.NOT_ACKNOWLEDGED


class PacketRouter:
    """
    Dispatches transport work through Gate.

    The `target` argument is retained for call-site compatibility and logging,
    but routing authority lives in Gate rather than in ENRICH peer URLs.
    """

    def __init__(
        self,
        gate_url: str,
        timeout: float = _ROUTE_TIMEOUT,
        *,
        domain_reader: DomainYamlReader | None = None,
        fallback_entity_type: str = "",
        fallback_id_property: str = "",
    ) -> None:
        """
        ``domain_reader`` resolves a domain's sync contract from its own spec
        (GRAPH-IDENT-02). The ``fallback_*`` pair is the clearly labeled
        stand-in for a domain whose spec declares none; both empty means fail
        closed for such a domain. A router built with neither can route
        anything except a graph sync.
        """
        self._client = build_gate_client(gate_url, timeout_seconds=timeout)
        self._gate_url = self._client.gate_url
        self._timeout_ms = int(timeout * 1000)
        self._domain_reader = domain_reader
        self._fallback_contract: GraphSyncContract | None = None
        if fallback_entity_type and fallback_id_property:
            self._fallback_contract = GraphSyncContract(
                entity_type=fallback_entity_type,
                id_property=fallback_id_property,
                source=GRAPH_SYNC_SOURCE_SETTINGS_FALLBACK,
            )
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def graph_sync_contract(
        self,
        domain: str | None,
        *,
        node_label: str | None = None,
    ) -> GraphSyncContract | None:
        """Resolve the CEG sync endpoint for ``domain``: spec first, then fallback.

        ``domain`` is the spec id. :meth:`notify_graph_sync` passes the tenant
        id (tenant id == domain spec id), the key CEG resolves the sync by.

        Three outcomes, in order:

        * the spec declares endpoints and one is selected -> that contract;
        * the spec declares endpoints but none can be selected (several
          endpoints, no label or ontology singles one out) -> ``None`` and
          ``graph_sync_skipped_ambiguous_contract`` names the domain and the
          candidates. The fallback is NOT consulted: a row of an entity whose
          type the spec could not name must not land on the fallback's
          endpoint, which belongs to another entity type;
        * the spec declares no endpoint at all -- no ``sync`` section, an
          unknown domain, an illegal id, an unreadable, malformed or
          wrong-shaped file -> the labeled settings fallback when configured,
          else ``None`` (fail closed). Spec problems are logged and never
          raised: a sync is a side effect, and the enrichment it follows
          already completed.

        An illegal id never reaches the filesystem: ``DomainYamlReader.load``
        refuses any id outside the one tenant grammar (``is_legal_tenant_id``)
        with ``ValueError`` before it builds a path, so ``../x`` cannot select a
        spec outside the domains root.
        """
        contract: GraphSyncContract | None = None
        spec = self._load_domain_spec(domain) if domain else None
        if spec is not None:
            contract = resolve_graph_sync_contract(spec, node_label=node_label)
            declared = _usable_sync_endpoints(spec) if contract is None else []
            if declared:
                logger.warning(
                    "graph_sync_skipped_ambiguous_contract",
                    domain=domain,
                    node_label=node_label,
                    candidates=[
                        {"entity_type": e, "id_property": i, "targetnode": t}
                        for e, i, t in declared
                    ],
                    fix="pass the entity's node label, or declare one endpoint per "
                    "ontology node so the label selects it",
                )
                return None
        if contract is None:
            if (
                self._domain_reader is not None
                and getattr(self._domain_reader, "require_published", False) is True
            ):
                return None
            contract = self._fallback_contract
        if contract is None:
            return None
        logger.info(
            "graph_sync_contract_resolved",
            domain=domain,
            entity_type=contract.entity_type,
            id_property=contract.id_property,
            source=contract.source,
        )
        return contract

    def _load_domain_spec(self, domain: str) -> dict[str, Any] | None:
        """The raw spec for ``domain``, ``{}`` for a non-mapping root, None when unreadable.

        None ("declares no endpoint") covers a missing reader, an unknown or
        illegal id, and an unreadable or malformed file; each is logged, never
        raised. AttributeError / TypeError / KeyError: an empty file (YAML
        `None`) or a list root makes DomainYamlReader._parse raise before any
        spec exists.
        """
        if self._domain_reader is None:
            return None
        try:
            config = self._domain_reader.load(domain)
            spec = config.raw_spec
        except (
            ValueError,
            OSError,
            yaml.YAMLError,
            AttributeError,
            TypeError,
            KeyError,
        ) as exc:
            if getattr(self._domain_reader, "require_published", False) is True or isinstance(
                exc, DomainPublicationError
            ):
                raise
            logger.warning(
                "graph_sync_domain_spec_unavailable",
                domain=domain,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None
        logger.info(
            "graph_sync_effective_revision",
            domain=domain,
            version=config.version,
            source_sha256=config.source_sha256,
            approval_ref=config.approval_ref,
        )
        return spec if isinstance(spec, dict) else {}

    async def route(
        self,
        target: NodeTarget,
        action: str,
        tenant_id: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
        *,
        idempotency_key: str | None = None,
        on_retry: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """
        Dispatch one logical operation through Gate.

        The SDK builds the root packet (destination Gate, EIE identity, one
        deadline, signing); this layer owns only the domain payload, the
        logical-operation identity, and the bounded retry policy above.
        ``on_retry`` is called before each retry: the earlier request's outcome
        is unknown and it may still be applied (graph_sync_intents.md rule 5).
        """
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._client.execute(
                    action=action,
                    payload=payload,
                    tenant=tenant_id,
                    idempotency_key=idempotency_key,
                    timeout_ms=self._timeout_ms,
                    correlation_id=correlation_id,
                    compliance_tags=("INTER_NODE",),
                )
                if _is_failure_packet(response):
                    # The worker executed and failed. Not retried here: the same
                    # packet would re-run the worker, and whether that is safe is
                    # the logical operation's (the caller's) decision, not the
                    # transport's. Gate did not cache this answer, so a caller's
                    # retry is not answered from a stale failure (SEAM-013).
                    logger.warning(
                        "packet_route_worker_failure",
                        target=target.value,
                        action=action,
                        packet_id=str(response.header.packet_id),
                        packet_type=response.header.packet_type,
                        detail=response.payload.get("message"),
                        attempt=attempt,
                    )
                    raise NodeOperationFailedError(
                        f"Gate-routed {action!r} for {target.value} answered a failure "
                        f"packet: {response.payload.get('error') or response.payload.get('code')}"
                    )
                data = dict(response.payload)
                data["packet_id"] = str(response.header.packet_id)
                logger.info(
                    "packet_routed",
                    target=target.value,
                    action=action,
                    packet_id=str(response.header.packet_id),
                    packet_type=response.header.packet_type,
                    gate_url=self._gate_url,
                    attempt=attempt,
                )
                return data

            except GateClientError as exc:
                last_exc = exc
                retry = attempt < _MAX_ATTEMPTS and _retry_allowed(
                    exc, has_idempotency_key=bool(idempotency_key)
                )
                logger.warning(
                    "packet_route_error",
                    target=target.value,
                    action=action,
                    attempt=attempt,
                    error=type(exc).__name__,
                    detail=str(exc),
                    will_retry=retry,
                )
                if not retry:
                    break
                if on_retry is not None:
                    on_retry()
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

        raise NodeUnreachableError(
            f"Gate-routed {action!r} for {target.value} failed: "
            f"{type(last_exc).__name__ if last_exc else 'unknown'}"
        ) from last_exc

    def route_fire_and_forget(
        self,
        target: NodeTarget,
        action: str,
        tenant_id: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        """
        Dispatch without awaiting. Errors are logged, never raised.
        Use for non-critical downstream notifications.
        """

        async def _safe_route() -> None:
            try:
                await self.route(
                    target,
                    action,
                    tenant_id,
                    payload,
                    correlation_id,
                    idempotency_key=idempotency_key,
                )
            except Exception as exc:
                logger.warning(
                    "fire_and_forget_failed",
                    target=target.value,
                    action=action,
                    error=str(exc),
                )

        task = asyncio.create_task(_safe_route())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def notify_graph_sync(
        self,
        tenant_id: str,
        entity_id: str,
        fields: dict[str, Any],
        domain: str | None = None,
        correlation_id: str | None = None,
        *,
        node_label: str | None = None,
        expected_contract_digest: str | None = None,
        on_outcome: Callable[[GraphSyncOutcome], None] | None = None,
        operation_key: str | None = None,
        sync_version: int | None = None,
    ) -> dict[str, Any] | None:
        """
        Sync enriched entity fields into the graph through Gate (action `sync`,
        owned by Cognitive.Engine.Graphs).

        The endpoint and id property come from the TENANT's own spec
        (:meth:`graph_sync_contract` keyed by ``tenant_id``, GRAPH-IDENT-02):
        tenant id == domain spec id, and CEG validates the sync against the
        spec it loads for the same tenant. ``domain`` does not select the spec
        and is not a row property (the row carries only the endpoint's declared
        properties); a ``domain`` other than the tenant is logged as
        ``graph_sync_domain_is_not_tenant``. The row is projected onto the
        endpoint's typed declaration (:func:`project_graph_sync_row`): a type
        mismatch is ``graph_sync_refused_row_type`` -- nothing is sent, None is
        returned -- and undeclared fields are dropped and counted. A tenant that resolves
        no contract is skipped -- nothing is sent and
        ``graph_sync_skipped_no_contract`` names it -- rather than addressed
        to an endpoint another tenant owns.

        Returns CEG's sync acknowledgment only when it confirms the intended
        operation (``graph_sync_acknowledged`` against the resolved endpoint),
        else None: a skipped domain, a Gate outage, an unresolvable route, a
        worker ``failure`` packet, a rejected batch, or a malformed answer are
        each a logged, bounded failure. There is no fallback path. ``None``
        means "not synced" — never "synced, response lost" (RV-DOMAIN-RD-07).

        ``on_outcome`` receives the attempt's :class:`GraphSyncOutcome` (A2): the
        same answer, classified for a caller that retries -- refused (terminal)
        versus retryable. ``expected_contract_digest`` pins the endpoint
        contract a durable intent was admitted against: when the tenant's
        current contract differs, nothing is sent and the attempt is refused as
        ``contract_changed`` rather than addressed to a contract the pinned
        fields were never checked against. ``operation_key`` binds the Gate
        idempotency key to the durable operation (:func:`_sync_idempotency_key`).
        ``sync_version`` is the durable version (the intent's ``seq``): sent as
        the row property ``sync_version`` when the endpoint declares it, which
        replaces any value of that name in ``fields``; otherwise not sent
        (graph_sync_intents.md rule 6).
        """

        retried: list[bool] = []

        def settle(
            kind: GraphSyncOutcomeKind, reason: str, ack: dict[str, Any] | None = None
        ) -> dict[str, Any] | None:
            if on_outcome is not None:
                on_outcome(
                    GraphSyncOutcome(kind=kind, reason=reason, ack=ack, retried=bool(retried))
                )
            return ack if kind is GraphSyncOutcomeKind.ACKNOWLEDGED else None

        if domain and domain != tenant_id:
            logger.warning(
                "graph_sync_domain_is_not_tenant",
                tenant_id=tenant_id,
                domain=domain,
                fix="the tenant's spec governs the sync (tenant id == domain spec id); "
                "the payload domain does not select it",
            )
        contract = self.graph_sync_contract(tenant_id, node_label=node_label)
        if contract is None:
            logger.warning(
                "graph_sync_skipped_no_contract",
                tenant_id=tenant_id,
                entity_id=entity_id,
                domain=domain,
                fix=_NO_CONTRACT_FIX,
            )
            return settle(GraphSyncOutcomeKind.REFUSED, "no_contract")
        if (
            expected_contract_digest is not None
            and graph_sync_contract_digest(contract) != expected_contract_digest
        ):
            logger.warning(
                "graph_sync_refused_contract_changed",
                tenant_id=tenant_id,
                entity_id=entity_id,
                entity_type=contract.entity_type,
                fix="the tenant's sync contract changed after this result committed; "
                "the pinned fields were admitted against the earlier contract and are not sent",
            )
            return settle(GraphSyncOutcomeKind.REFUSED, "contract_changed")
        fields = _with_sync_version(fields, sync_version, contract)
        projection = project_graph_sync_row(entity_id=entity_id, fields=fields, contract=contract)
        if projection.dropped:
            logger.info(
                "graph_sync_undeclared_fields_dropped",
                tenant_id=tenant_id,
                entity_id=entity_id,
                entity_type=contract.entity_type,
                dropped_count=len(projection.dropped),
                dropped=list(projection.dropped),
            )
        row = projection.row
        if row is None:
            logger.warning(
                "graph_sync_refused_row_type",
                tenant_id=tenant_id,
                entity_id=entity_id,
                entity_type=contract.entity_type,
                defects=[
                    {
                        "field": d.field,
                        "declared": d.declared,
                        "actual": d.actual,
                        "reason": d.reason,
                    }
                    for d in projection.defects
                ],
                fix="send values of each property's declared type (domains/<tenant>/spec.yaml); "
                "nothing was sent and nothing is recorded as synced",
            )
            return settle(GraphSyncOutcomeKind.REFUSED, "row_type")
        batch = [row]
        try:
            ack = await self.route(
                NodeTarget.GRAPH,
                "sync",
                tenant_id,
                {"entity_type": contract.entity_type, "batch": batch},
                correlation_id,
                idempotency_key=_sync_idempotency_key(tenant_id, entity_id, row, operation_key),
                on_retry=lambda: retried.append(True),
            )
        except NodeUnreachableError as exc:
            logger.warning("graph_sync_failed", entity_id=entity_id, error=str(exc))
            return settle(graph_sync_failure_kind(exc), f"route:{type(exc).__name__}")
        # CEG answers synced_count == len(batch); an acknowledgment for any other
        # count cannot be this request's and does not confirm this row.
        if not graph_sync_acknowledged(
            ack, entity_type=contract.entity_type, expected_count=len(batch)
        ):
            logger.warning(
                "graph_sync_unacknowledged",
                entity_id=entity_id,
                status=ack.get("status"),
                synced_count=ack.get("synced_count"),
                entity_type=ack.get("entity_type"),
                expected_entity_type=contract.entity_type,
            )
            return settle(*_unconfirmed_outcome(ack, contract.entity_type))
        return settle(GraphSyncOutcomeKind.ACKNOWLEDGED, "acknowledged", ack)

    def graph_sync_pin(
        self, tenant_id: str, *, node_label: str | None = None
    ) -> GraphSyncPin | None:
        """The endpoint a graph intent for ``tenant_id`` is pinned to now, or None.

        None when no contract resolves; the intent is then recorded unpinned and
        its dispatch refuses as ``no_contract`` if none resolves then either.
        """
        contract = self.graph_sync_contract(tenant_id, node_label=node_label)
        if contract is None:
            return None
        return GraphSyncPin(
            entity_type=contract.entity_type,
            contract_digest=graph_sync_contract_digest(contract),
        )

    def relationship_sync_contract(
        self,
        tenant_id: str,
        assertion: RelationshipAssertion,
    ) -> RelationshipSyncContract | None:
        """Resolve the tenant's declared endpoint for ``assertion``'s relationship."""
        spec = self._load_domain_spec(tenant_id)
        if spec is None:
            return None
        return resolve_relationship_sync_contract(
            spec,
            relation_type=assertion.relation_type,
            from_label=assertion.subject_label,
            to_label=assertion.object_label,
        )

    def observed_association_contract(
        self,
        tenant_id: str,
        *,
        provider: str,
        from_object_type: str,
        to_object_type: str,
        association_category: str,
        association_type_id: int,
    ) -> ObservedRelationshipContract | None:
        """The tenant relationship one provider association is declared for, else None.

        Read from the tenant's spec (``sync.endpoints[].observedfrom`` and the
        node endpoints' ``refnamespace``); nothing is defaulted and there is no
        settings fallback. Every refusal is logged with its reason and a fix.
        """
        # Imported here: the resolver reuses this module's relationship contract.
        from app.engines.observed_association_contract import (
            resolve_observed_association_contract,
        )

        spec = self._load_domain_spec(tenant_id)
        if spec is None:
            logger.warning(
                "observed_association_skipped_no_spec",
                tenant_id=tenant_id,
                fix="provide domains/<tenant_id>/spec.yaml mirroring CEG's tenant spec",
            )
            return None
        resolution = resolve_observed_association_contract(
            spec,
            provider=provider,
            from_object_type=from_object_type,
            to_object_type=to_object_type,
            association_category=association_category,
            association_type_id=association_type_id,
        )
        if resolution.contract is None:
            failure = resolution.failure.value if resolution.failure else "unknown"
            logger.warning(
                f"observed_association_skipped_{failure}",
                tenant_id=tenant_id,
                provider=provider,
                detail=resolution.detail,
                fix="declare exactly one sync.endpoints[].observedfrom entry for this association "
                "on a valid relationship endpoint, and refnamespace on both node endpoints",
            )
        return resolution.contract

    async def notify_relationship_sync(
        self,
        tenant_id: str,
        assertion: RelationshipAssertion,
        correlation_id: str | None = None,
    ) -> RelationshipSyncResult:
        """Propose one evidenced relationship to the graph through Gate ``sync``.

        The endpoint comes from the TENANT's spec (tenant id == domain spec id),
        exactly as for node sync; there is no settings fallback for a
        relationship. Always answers a typed ``RelationshipSyncResult``; only
        ``ACKNOWLEDGED`` (CEG's relationship endpoint confirmed this row,
        ``relationship_sync_acknowledged``) carries the ack and means synced.
        An incomplete assertion or an undeclared or mismatched relationship is
        ``NOT_PROPOSED``; a Gate outage after retries is ``TRANSPORT_UNAVAILABLE``;
        a worker failure, a 4xx or any unconfirming answer is ``NOT_ACKNOWLEDGED``.
        A retry with the same assertion reuses the same idempotency key.
        """
        defects = relationship_assertion_defects(assertion)
        if defects:
            logger.warning(
                "relationship_sync_skipped_incomplete_assertion",
                tenant_id=tenant_id,
                evidence_ref=assertion.evidence_ref,
                defects=defects,
            )
            return RelationshipSyncResult(
                RelationshipSyncOutcome.NOT_PROPOSED, "incomplete assertion: " + "; ".join(defects)
            )
        contract = self.relationship_sync_contract(tenant_id, assertion)
        if contract is None:
            logger.warning(
                "relationship_sync_skipped_no_contract",
                tenant_id=tenant_id,
                relation_type=assertion.relation_type,
                from_label=assertion.subject_label,
                to_label=assertion.object_label,
                fix="declare a sync endpoint with this targetedge/fromnode/tonode and the "
                "matching DIRECTED, managedby: sync edge in ontology.edges",
            )
            return RelationshipSyncResult(
                RelationshipSyncOutcome.NOT_PROPOSED,
                "tenant declares no sync endpoint for this relationship",
            )
        return await self._send_relationship(tenant_id, assertion, contract, correlation_id)

    async def _send_relationship(
        self,
        tenant_id: str,
        assertion: RelationshipAssertion,
        contract: RelationshipSyncContract,
        correlation_id: str | None,
    ) -> RelationshipSyncResult:
        """Route one relationship row through Gate and classify the answer."""
        row = _relationship_sync_row(assertion)
        try:
            ack = await self.route(
                NodeTarget.GRAPH,
                "sync",
                tenant_id,
                {"entity_type": contract.entity_type, "batch": [row]},
                correlation_id,
                idempotency_key=_relationship_idempotency_key(tenant_id, contract, row),
            )
        except NodeUnreachableError as exc:
            outcome = relationship_failure_outcome(exc)
            logger.warning(
                "relationship_sync_failed",
                evidence_ref=assertion.evidence_ref,
                outcome=outcome.value,
                error=str(exc),
            )
            return RelationshipSyncResult(outcome, str(exc))
        if not relationship_sync_acknowledged(
            ack, entity_type=contract.entity_type, expected_count=1
        ):
            logger.warning(
                "relationship_sync_unacknowledged",
                evidence_ref=assertion.evidence_ref,
                status=ack.get("status"),
                entity_type=ack.get("entity_type"),
                expected_entity_type=contract.entity_type,
            )
            return RelationshipSyncResult(
                RelationshipSyncOutcome.NOT_ACKNOWLEDGED,
                f"answer does not confirm the row on endpoint {contract.entity_type!r} "
                f"(status {ack.get('status')!r})",
            )
        return RelationshipSyncResult(
            RelationshipSyncOutcome.ACKNOWLEDGED, "acknowledged by CEG", ack
        )

    async def close(self) -> None:
        """No-op close; GateClient is request-scoped."""
        return None


_router_singleton: PacketRouter | None = None


#: Environments allowed to read checked-in, unpublished domain specs. Mirrors
#: the startup reader in app/main.py (lifespan ``DomainYamlReader``); every
#: other environment serves tenants and requires native publication (F-A1-09).
UNPUBLISHED_SPEC_ENVIRONMENTS = frozenset({"local", "dev", "test"})
_SETTINGS_DEFAULT_ENVIRONMENT = "local"  # app.core.config.Settings.l9_environment


def requires_published_specs(settings: Any) -> bool:
    """Whether the graph-sync reader must refuse unpublished specs, as startup does.

    A settings object without ``l9_environment`` takes ``Settings``' own default
    (``"local"``); any value that is not a string requires publication.
    """
    environment = getattr(settings, "l9_environment", _SETTINGS_DEFAULT_ENVIRONMENT)
    return not (isinstance(environment, str) and environment in UNPUBLISHED_SPEC_ENVIRONMENTS)


def get_router(settings: Any) -> PacketRouter:
    """Module-level singleton router. One instance per process.

    The router reads each domain's sync contract from ``settings.domains_dir``
    -- the same directory the converge path reads, so one spec per tenant
    governs both. ``Settings.graph_sync_*`` is the labeled fallback; a settings
    object without those attributes gets none and fails closed for a domain
    whose spec declares no endpoint.
    """
    global _router_singleton
    if _router_singleton is not None:
        return _router_singleton
    domains_dir = getattr(settings, "domains_dir", None)
    _router_singleton = PacketRouter(
        gate_url=settings.gate_url,
        domain_reader=(
            DomainYamlReader(domains_dir, require_published=requires_published_specs(settings))
            if domains_dir
            else None
        ),
        fallback_entity_type=getattr(settings, "graph_sync_entity_type", "") or "",
        fallback_id_property=getattr(settings, "graph_sync_id_property", "") or "",
    )
    return _router_singleton
