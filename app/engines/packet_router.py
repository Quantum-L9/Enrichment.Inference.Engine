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
* GRAPH-IDENT-02: the endpoint and its id property are read per domain from
  that domain's spec (``sync.endpoints[]``, CEG's owner-native ``path`` /
  ``targetnode`` / ``idproperty`` keys), never from a process-global constant.
  A domain whose spec declares no resolvable endpoint falls back to the
  clearly labeled ``Settings.graph_sync_*`` pair when it is set, and is
  otherwise skipped (fail closed: nothing is sent, a warning names the domain).
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
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog
import yaml
from constellation_node_sdk.gate import (
    GateClientError,
    GateConnectionError,
    GateHTTPError,
    GateTimeoutError,
)

from app.engines.domain_yaml_reader import DomainYamlReader
from app.services.gate_client import EIE_NODE_NAME, build_gate_client

logger = structlog.get_logger("packet_router")

# Per-attempt operation budget. Graph sync runs inside EIE's own inbound budget
# when triggered by a Gate-dispatched `enrich`, so it must stay well under the
# node cap EIE advertises (25 s): two attempts plus backoff fit inside it.
_ROUTE_TIMEOUT = 10.0
_MAX_ATTEMPTS = 2
_BACKOFF_BASE_SECONDS = 1.0

_PRIMITIVE = (str, int, float, bool, type(None))

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
    on) and ``id_property`` the row key that endpoint merges on.
    """

    entity_type: str
    id_property: str
    source: str


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

    1. ``node_label`` given -> the endpoint whose ``targetnode`` is that label;
    2. exactly one usable endpoint -> that endpoint;
    3. exactly one endpoint whose ``targetnode`` is a label of this domain's
       own ontology -> that endpoint.

    Anything else -- no ``sync`` section, no endpoint with both a path and an
    id property, or several candidates with no label to choose by -- is
    ``None``. The caller decides between fallback and fail-closed; this
    function never guesses.
    """
    usable = _usable_sync_endpoints(domain_spec)
    if not usable:
        return None
    if node_label:
        chosen = [c for c in usable if c[2] == node_label]
    elif len(usable) == 1:
        chosen = usable
    else:
        labels = _ontology_labels(domain_spec)
        chosen = [c for c in usable if c[2] in labels]
    if len(chosen) != 1:
        return None
    entity_type, id_property, _target = chosen[0]
    return GraphSyncContract(
        entity_type=entity_type,
        id_property=id_property,
        source=GRAPH_SYNC_SOURCE_DOMAIN_SPEC,
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
    ``expected_count`` when the caller knows its batch size, or an
    acknowledgment for a different endpoint — is NOT a synchronization. The
    graph may or may not hold the row; the caller must not record it as synced.
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
    return not (entity_type is not None and ack.get("entity_type") != entity_type)


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


def _graph_sync_row(
    *,
    entity_id: str,
    fields: dict[str, Any],
    domain: str | None,
    id_property: str,
) -> dict[str, Any]:
    """Project enriched fields onto one CEG sync row.

    CEG applies ``SET n += row`` in Cypher, so every value must be a Neo4j
    property primitive. Nested structures are carried as JSON strings rather
    than dropped, so nothing the enrichment produced is silently lost.
    """
    row: dict[str, Any] = {id_property: entity_id, "entity_id": entity_id}
    if domain:
        row["domain"] = domain
    for key, value in fields.items():
        if key in {id_property, "entity_id"}:
            continue
        if (
            isinstance(value, _PRIMITIVE)
            or isinstance(value, list)
            and all(isinstance(v, _PRIMITIVE) for v in value)
        ):
            row[key] = value
        else:
            row[key] = json.dumps(value, sort_keys=True, default=str)
    row["enriched_by"] = EIE_NODE_NAME
    return row


def _sync_idempotency_key(tenant_id: str, entity_id: str, row: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(row, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"eie:sync:{tenant_id}:{entity_id}:{digest}"


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

        Returns None when neither the domain's spec nor the configured fallback
        yields a contract -- the fail-closed case ``notify_graph_sync`` honours
        by sending nothing. Spec problems (unknown domain, illegal id, unreadable
        or malformed file) are logged and treated as "declares none", never
        raised: a sync is a side effect, and the enrichment it follows already
        completed.
        """
        contract: GraphSyncContract | None = None
        if domain and self._domain_reader is not None:
            try:
                spec = self._domain_reader.load(domain).raw_spec
            except (FileNotFoundError, ValueError, OSError, yaml.YAMLError) as exc:
                logger.warning("graph_sync_domain_spec_unavailable", domain=domain, error=str(exc))
            else:
                contract = resolve_graph_sync_contract(spec, node_label=node_label)
        if contract is None:
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

    async def route(
        self,
        target: NodeTarget,
        action: str,
        tenant_id: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """
        Dispatch one logical operation through Gate.

        The SDK builds the root packet (destination Gate, EIE identity, one
        deadline, signing); this layer owns only the domain payload, the
        logical-operation identity, and the bounded retry policy above.
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
    ) -> dict[str, Any] | None:
        """
        Sync enriched entity fields into the graph through Gate (action `sync`,
        owned by Cognitive.Engine.Graphs).

        The endpoint and id property come from ``domain``'s own spec
        (:meth:`graph_sync_contract`, GRAPH-IDENT-02). A domain that resolves
        no contract is skipped -- nothing is sent and
        ``graph_sync_skipped_no_contract`` names it -- rather than addressed
        to an endpoint another tenant owns.

        Returns CEG's sync acknowledgment only when it confirms the intended
        operation (``graph_sync_acknowledged`` against the resolved endpoint),
        else None: a skipped domain, a Gate outage, an unresolvable route, a
        worker ``failure`` packet, a rejected batch, or a malformed answer are
        each a logged, bounded failure. There is no fallback path. ``None``
        means "not synced" — never "synced, response lost" (RV-DOMAIN-RD-07).
        """
        contract = self.graph_sync_contract(domain, node_label=node_label)
        if contract is None:
            logger.warning(
                "graph_sync_skipped_no_contract",
                tenant_id=tenant_id,
                entity_id=entity_id,
                domain=domain,
                fix=_NO_CONTRACT_FIX,
            )
            return None
        row = _graph_sync_row(
            entity_id=entity_id,
            fields=fields,
            domain=domain,
            id_property=contract.id_property,
        )
        batch = [row]
        try:
            ack = await self.route(
                NodeTarget.GRAPH,
                "sync",
                tenant_id,
                {"entity_type": contract.entity_type, "batch": batch},
                correlation_id,
                idempotency_key=_sync_idempotency_key(tenant_id, entity_id, row),
            )
        except NodeUnreachableError as exc:
            logger.warning("graph_sync_failed", entity_id=entity_id, error=str(exc))
            return None
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
            return None
        return ack

    async def notify_score_invalidate(
        self,
        tenant_id: str,
        entity_id: str,
        domain: str | None = None,
    ) -> None:
        """Fire-and-forget score invalidation after entity enrichment."""
        self.route_fire_and_forget(
            NodeTarget.SCORE,
            "score-invalidate",
            tenant_id,
            {"entity_id": entity_id, "domain": domain or ""},
        )

    async def close(self) -> None:
        """No-op close; GateClient is request-scoped."""
        return None


_router_singleton: PacketRouter | None = None


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
        domain_reader=DomainYamlReader(domains_dir) if domains_dir else None,
        fallback_entity_type=getattr(settings, "graph_sync_entity_type", "") or "",
        fallback_id_property=getattr(settings, "graph_sync_id_property", "") or "",
    )
    return _router_singleton
