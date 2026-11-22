"""
GRAPH -> ENRICH return channel for inference results.

Receives GRAPH inference outputs as `TransportPacket` instances and converts
them into `EnrichmentTarget` records for injection into the convergence loop.

A4: where a durable store backs the process (`pg_store` initialised), every
output is recorded in `graph_return_targets` in one transaction before the
packet is acknowledged, and a later converge of its tenant and entity leases
and settles it (docs/contracts/data/graph_return_intake.md). Without a store
the per-process queue below is the best-effort fallback the single-replica
deployment pins (tests/unit/test_worker_singleton_invariant.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import threading
import time
import uuid
from collections import Counter, OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from constellation_node_sdk.transport import TransportPacket, create_transport_packet
from constellation_node_sdk.transport.errors import TransportValidationError

from app.utils.safe_convert import safe_float

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnrichmentTarget:
    """A re-enrichment directive produced by a GRAPH inference output."""

    entity_id: str
    tenant_id: str
    field_name: str
    seed_value: Any
    source_confidence: float
    origin_packet_id: str
    origin_inference_rule: str
    created_at: float = field(default_factory=time.time)
    # The durable row this target was leased from (A4); None from the
    # in-process queue. Internal: never part of to_dict or any payload.
    intake_id: str | None = field(default=None, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "tenant_id": self.tenant_id,
            "field_name": self.field_name,
            "seed_value": self.seed_value,
            "source_confidence": self.source_confidence,
            "origin_packet_id": self.origin_packet_id,
            "origin_inference_rule": self.origin_inference_rule,
            "created_at": self.created_at,
        }


CONFIDENCE_FLOOR = 0.55

#: How long a retained target waits for a converge of its entity (A4).
GRAPH_RETURN_TTL_SECONDS = 7 * 24 * 3600.0
#: How long one converge holds the targets it leased before another may.
GRAPH_RETURN_LEASE_SECONDS = 300.0
#: Open (retained or leased) targets one tenant may hold durably.
GRAPH_RETURN_TENANT_CAPACITY = 10_000

# Rejection reasons recorded for an output that is not retained.
REJECT_MALFORMED = "malformed"
REJECT_CROSS_TENANT = "cross_tenant"
REJECT_BELOW_THRESHOLD = "below_threshold"
REJECT_FIELD_NOT_IN_SCHEMA = "field_not_in_schema"


class GraphReturnCapacityError(RuntimeError):
    """The tenant cannot take this packet's targets now; nothing was accepted.

    Under the SDK runtime's error semantics this is an internal error (HTTP
    500 / a failure packet), which a producer retries -- never the `accepted`
    answer a dropped target used to get.
    """

    def __init__(self, tenant_id: str, open_count: int, incoming: int, capacity: int) -> None:
        super().__init__(
            f"graph_return_capacity: tenant {tenant_id!r} holds {open_count} open targets; "
            f"{incoming} more would exceed {capacity}"
        )
        self.tenant_id = tenant_id
        self.open_count = open_count
        self.incoming = incoming
        self.capacity = capacity


@dataclass(frozen=True, slots=True)
class GraphReturnRejection:
    """One output the intake did not accept, and why."""

    index: int
    reason: str
    entity_id: str | None = None
    field_name: str | None = None


@dataclass(frozen=True, slots=True)
class GraphReturnClaim:
    """The targets one converge pass leased, and the token that settles them."""

    owner: str | None
    targets: list[EnrichmentTarget]

    @property
    def durable(self) -> bool:
        return self.owner is not None


def validate_graph_inference_packet(packet: TransportPacket) -> None:
    """Validate graph inference packet semantics on top of SDK validation."""
    if packet.header.action != "graph-inference-result":
        msg = f"expected action='graph-inference-result', got {packet.header.action!r}"
        raise TransportValidationError(msg)

    outputs = packet.payload.get("inference_outputs")
    if not isinstance(outputs, list):
        raise TransportValidationError("'inference_outputs' must be a list")


def producer_key(packet: TransportPacket) -> str:
    """The producer's idempotency key: the header's, else the packet id.

    A Gate retransmit carries the same packet, so either is stable across it.
    """
    key = packet.header.idempotency_key
    return str(key) if key else f"packet:{packet.header.packet_id}"


def _identity(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text or None


def _confidence(output: Mapping[str, Any]) -> float | None:
    """The output's confidence in [0, 1]; None when it is not one."""
    raw = output.get("confidence", 0.0)
    if isinstance(raw, bool):
        return None
    confidence = safe_float(raw, default=math.nan)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None
    return confidence


def _classify_output(
    index: int, output: Any, tenant_id: str
) -> tuple[dict[str, Any] | None, GraphReturnRejection | None]:
    """Bind one output, or say why it is not a target."""
    if not isinstance(output, Mapping):
        return None, GraphReturnRejection(index, REJECT_MALFORMED)
    entity_id = _identity(output.get("entity_id"))
    field_name = _identity(output.get("field"))
    if entity_id is None or field_name is None:
        return None, GraphReturnRejection(index, REJECT_MALFORMED, entity_id, field_name)
    confidence = _confidence(output)
    if confidence is None:
        return None, GraphReturnRejection(index, REJECT_MALFORMED, entity_id, field_name)
    bound = {
        "entity_id": entity_id,
        "field_name": field_name,
        "seed_value": output.get("value"),
        "source_confidence": confidence,
        "origin_inference_rule": _identity(output.get("rule")) or "unknown",
    }
    # Tenant comes only from the Gate packet (EIE-SEC-01). An output naming
    # another tenant is refused -- recorded under the packet's tenant -- and
    # never re-homed.
    for key in ("tenant_id", "tenant"):
        claimed = output.get(key)
        if claimed is not None and str(claimed) != tenant_id:
            return bound, GraphReturnRejection(index, REJECT_CROSS_TENANT, entity_id, field_name)
    if confidence < CONFIDENCE_FLOOR:
        logger.debug(
            "Skipping low-confidence inference output (%.3f < %.3f) for entity=%s field=%s",
            confidence,
            CONFIDENCE_FLOOR,
            entity_id,
            field_name,
        )
        return bound, GraphReturnRejection(index, REJECT_BELOW_THRESHOLD, entity_id, field_name)
    return bound, None


def classify_packet(
    packet: TransportPacket,
) -> tuple[list[EnrichmentTarget], list[GraphReturnRejection], list[dict[str, Any]]]:
    """Split a validated packet into eligible targets and refused outputs.

    The third list holds every output that is identifiable (entity and field
    bound), eligible or not, with its rejection reason (`None` when eligible):
    what the durable intake records.
    """
    validate_graph_inference_packet(packet)
    tenant_id = packet.tenant.org_id
    packet_id = str(packet.header.packet_id)
    targets: list[EnrichmentTarget] = []
    rejections: list[GraphReturnRejection] = []
    recordable: list[dict[str, Any]] = []
    for index, output in enumerate(packet.payload.get("inference_outputs", [])):
        bound, rejection = _classify_output(index, output, tenant_id)
        if rejection is not None:
            rejections.append(rejection)
        if bound is None:
            continue
        recordable.append({**bound, "rejection": rejection.reason if rejection else None})
        if rejection is None:
            targets.append(
                EnrichmentTarget(tenant_id=tenant_id, origin_packet_id=packet_id, **bound)
            )
    return targets, rejections, recordable


def extract_targets_from_packet(packet: TransportPacket) -> list[EnrichmentTarget]:
    """Convert validated inference outputs to return-channel targets.

    An output that is malformed, names another tenant, or falls below the
    confidence floor is not a target (it is logged, and recorded by the
    durable intake); it never fails the whole packet.
    """
    targets, _, _ = classify_packet(packet)
    return targets


def intake_key(packet_key: str, target: EnrichmentTarget) -> str:
    """The producer's idempotency key plus (tenant, entity, field, rule, value)."""
    value = json.dumps(target.seed_value, sort_keys=True, default=str, separators=(",", ":"))
    material = "|".join(
        (
            packet_key,
            target.tenant_id,
            target.entity_id,
            target.field_name,
            target.origin_inference_rule,
            value,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _durable_store() -> Any | None:
    """`pg_store` when it backs this process, else None (no DB runtime or engine)."""
    try:
        from app.services import pg_store
    except ImportError:  # no DB runtime installed: nothing durable exists
        return None
    return pg_store if pg_store._engine is not None else None


def _log_rejections(tenant_id: str, packet_id: str, rejections: list[GraphReturnRejection]) -> None:
    if not rejections:
        return
    reasons = Counter(r.reason for r in rejections)
    logger.info(
        "graph_return_outputs_rejected tenant=%s packet=%s reasons=%s",
        tenant_id,
        packet_id,
        dict(sorted(reasons.items())),
    )


class GraphReturnChannel:
    """
    Async queue bridging GRAPH inference outputs back to ENRICH convergence loop.

    Usage (SDK handler):
        channel = GraphReturnChannel.get_instance()
        await channel.submit(packet)

    Usage (convergence_controller, at the start of each pass):
        targets = channel.take_for_entity(tenant_id="acme", entity_id="res.partner:55")
    """

    _instance: GraphReturnChannel | None = None

    def __init__(self, maxsize: int = 10_000) -> None:
        self._queues: dict[str, asyncio.Queue[EnrichmentTarget]] = {}
        # Queued targets per entity, per tenant. take_for_entity reads this
        # first so a converge pass for an entity with nothing queued is O(1),
        # however large the tenant's backlog is.
        self._pending: dict[str, Counter[str]] = {}
        # Intake keys this process already accepted (bounded): a retransmit
        # of one packet queues its targets once. Best effort -- the durable
        # intake is what survives a restart.
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_limit = max(2 * maxsize, 1)
        self._maxsize = maxsize
        self._submitted: int = 0
        self._drained: int = 0
        self._rejected: int = 0
        self._refused_packets: int = 0
        self._duplicates: int = 0

    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> GraphReturnChannel:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Test helper — reset the singleton."""
        cls._instance = None

    def _queue_for(self, tenant_id: str) -> asyncio.Queue[EnrichmentTarget]:
        if tenant_id not in self._queues:
            self._queues[tenant_id] = asyncio.Queue(maxsize=self._maxsize)
        return self._queues[tenant_id]

    async def submit(self, packet: TransportPacket) -> int:
        """
        Validate the packet, bind its outputs, and accept the eligible ones.

        Returns the number of this packet's targets accepted for a later
        converge -- the same number when the packet is delivered again.
        Raises TransportValidationError if the packet is invalid, and
        GraphReturnCapacityError, accepting nothing, when the tenant cannot
        take every eligible target: a packet is never half-accepted.
        """
        targets, rejections, recordable = classify_packet(packet)
        tenant_id = packet.tenant.org_id
        packet_id = str(packet.header.packet_id)
        self._rejected += len(rejections)
        _log_rejections(tenant_id, packet_id, rejections)
        store = _durable_store()
        if store is not None:
            return await self._retain(store, packet, recordable)
        return self._enqueue(packet, targets)

    async def _retain(
        self, store: Any, packet: TransportPacket, recordable: list[dict[str, Any]]
    ) -> int:
        """Durable intake: one transaction for the whole packet (A4)."""
        tenant_id = packet.tenant.org_id
        key = producer_key(packet)
        intakes = [
            store.GraphReturnIntake(
                tenant_id=tenant_id,
                entity_id=item["entity_id"],
                field_name=item["field_name"],
                rule=item["origin_inference_rule"],
                value=item["seed_value"],
                confidence=item["source_confidence"],
                producer_key=key,
                origin_packet_id=str(packet.header.packet_id),
                rejection=item["rejection"],
            )
            for item in recordable
        ]
        try:
            result = await store.retain_graph_return_targets(
                intakes,
                capacity=GRAPH_RETURN_TENANT_CAPACITY,
                ttl_seconds=GRAPH_RETURN_TTL_SECONDS,
            )
        except GraphReturnCapacityError:
            self._refused_packets += 1
            logger.warning(
                "GraphReturnChannel: refused packet=%s for tenant=%s at capacity",
                packet.header.packet_id,
                tenant_id,
            )
            raise
        self._submitted += result.retained
        self._duplicates += result.duplicates
        logger.info(
            "GraphReturnChannel: retained %d targets for tenant=%s (packet=%s, duplicates=%d, "
            "unchanged=%d)",
            result.retained,
            tenant_id,
            packet.header.packet_id,
            result.duplicates,
            result.unchanged,
        )
        return int(result.retained)

    def _enqueue(self, packet: TransportPacket, targets: list[EnrichmentTarget]) -> int:
        """In-process fallback: all of the packet's new targets, or none."""
        tenant_id = packet.tenant.org_id
        key = producer_key(packet)
        keyed = [(intake_key(key, t), t) for t in targets]
        fresh: list[tuple[str, EnrichmentTarget]] = []
        batch: set[str] = set()
        for k, target in keyed:
            if k in self._seen or k in batch:
                continue
            batch.add(k)
            fresh.append((k, target))
        q = self._queue_for(tenant_id)
        if len(fresh) > q.maxsize - q.qsize():
            self._refused_packets += 1
            logger.warning(
                "EnrichmentTargetQueue full for tenant=%s: refusing packet=%s (%d targets, "
                "%d free)",
                tenant_id,
                packet.header.packet_id,
                len(fresh),
                q.maxsize - q.qsize(),
            )
            raise GraphReturnCapacityError(tenant_id, q.qsize(), len(fresh), q.maxsize)
        for k, target in fresh:
            q.put_nowait(target)
            self._pending.setdefault(tenant_id, Counter())[target.entity_id] += 1
            self._remember(k)
        self._submitted += len(fresh)
        self._duplicates += len(keyed) - len(fresh)
        logger.info(
            "GraphReturnChannel: submitted %d targets for tenant=%s (packet=%s)",
            len(fresh),
            tenant_id,
            packet.header.packet_id,
        )
        # A retransmit is answered with what the packet got accepted.
        return len({k for k, _ in keyed})

    def _remember(self, key: str) -> None:
        self._seen[key] = None
        while len(self._seen) > self._seen_limit:
            self._seen.popitem(last=False)

    async def claim_for_entity(
        self,
        tenant_id: str,
        entity_id: str,
        *,
        owner: str = "converge",
        max_targets: int = 500,
    ) -> GraphReturnClaim:
        """Lease this entity's accepted targets to one converge pass.

        Durable: the rows are leased under a fencing token (two workers never
        lease one row); a converge that dies leaves them to its lease's
        expiry. In-process: the queued targets are taken (take_for_entity).
        """
        store = _durable_store()
        if store is None:
            return GraphReturnClaim(
                owner=None,
                targets=self.take_for_entity(tenant_id, entity_id, max_targets=max_targets),
            )
        records = await store.claim_graph_return_targets(
            tenant_id=tenant_id,
            entity_id=entity_id,
            owner=owner,
            lease_seconds=GRAPH_RETURN_LEASE_SECONDS,
            limit=max_targets,
        )
        self._drained += len(records)
        token = records[0].lease_owner if records else None
        return GraphReturnClaim(
            owner=token,
            targets=[
                EnrichmentTarget(
                    entity_id=r.entity_id,
                    tenant_id=r.tenant_id,
                    field_name=r.field_name,
                    seed_value=r.value,
                    source_confidence=r.confidence,
                    origin_packet_id=r.origin_packet_id,
                    origin_inference_rule=r.rule,
                    created_at=r.created_at.timestamp(),
                    intake_id=str(r.id),
                )
                for r in records
            ],
        )

    async def settle(
        self,
        claim: GraphReturnClaim,
        *,
        outcomes: Mapping[str, str],
        rejections: Mapping[str, str],
        consumed_by: str | None,
    ) -> int:
        """Record what the converge did with a durable claim's targets.

        `outcomes` and `rejections` are keyed by `EnrichmentTarget.intake_id`.
        A target of the claim named in neither is left to its lease's expiry,
        to be offered again. An in-process claim has nothing to record.
        """
        if not claim.durable or not claim.targets:
            return 0
        store = _durable_store()
        if store is None:
            logger.warning("GraphReturnChannel: durable store gone before settle")
            return 0
        held = {t.intake_id for t in claim.targets}
        return int(
            await store.settle_graph_return_targets(
                owner=claim.owner,
                consumed={uuid.UUID(k): v for k, v in outcomes.items() if k in held},
                rejected={uuid.UUID(k): v for k, v in rejections.items() if k in held},
                consumed_by=consumed_by,
            )
        )

    async def drain(
        self,
        tenant_id: str,
        *,
        timeout: float = 0.1,
        max_targets: int = 500,
    ) -> list[EnrichmentTarget]:
        """
        Non-blocking drain: collect up to max_targets from the tenant queue.
        Called by convergence_controller at the start of each new pass.
        """
        q = self._queue_for(tenant_id)
        targets: list[EnrichmentTarget] = []
        deadline = time.monotonic() + timeout
        while len(targets) < max_targets:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                target = await asyncio.wait_for(q.get(), timeout=remaining)
                targets.append(target)
                q.task_done()
            except TimeoutError:
                break
        self._drained += len(targets)
        self._forget(tenant_id, targets)
        if targets:
            logger.info(
                "GraphReturnChannel: drained %d targets for tenant=%s",
                len(targets),
                tenant_id,
            )
        return targets

    def take_for_entity(
        self,
        tenant_id: str,
        entity_id: str,
        *,
        max_targets: int = 500,
    ) -> list[EnrichmentTarget]:
        """Remove and return the queued targets for one entity of one tenant.

        This is the convergence loop's consumer (EIE-POST-F001). It never
        blocks, so it costs the request deadline nothing. Targets for other
        entities of the same tenant go back on the queue in their original
        order, for the loop converging those entities. It contains no await,
        so no submit can interleave while the queue is being partitioned.

        The queue is scanned only when this entity has targets pending, so an
        entity with nothing queued costs O(1) per pass; one that has targets
        pays one scan, after which its later passes are O(1) again.
        """
        q = self._queues.get(tenant_id)
        if q is None or not self._pending.get(tenant_id, Counter())[entity_id]:
            return []
        taken: list[EnrichmentTarget] = []
        kept: list[EnrichmentTarget] = []
        for _ in range(q.qsize()):
            target = q.get_nowait()
            q.task_done()
            if target.entity_id == entity_id and len(taken) < max_targets:
                taken.append(target)
            else:
                kept.append(target)
        for target in kept:
            q.put_nowait(target)
        self._drained += len(taken)
        self._forget(tenant_id, taken)
        if taken:
            logger.info(
                "GraphReturnChannel: took %d targets for tenant=%s entity=%s",
                len(taken),
                tenant_id,
                entity_id,
            )
        return taken

    def _forget(self, tenant_id: str, targets: list[EnrichmentTarget]) -> None:
        """Remove consumed targets from the per-entity pending counts."""
        pending = self._pending.get(tenant_id)
        if pending is None:
            return
        pending.subtract(t.entity_id for t in targets)
        for entity_id in [e for e, n in pending.items() if n <= 0]:
            del pending[entity_id]

    def stats(self) -> dict[str, Any]:
        return {
            "submitted": self._submitted,
            "drained": self._drained,
            "rejected": self._rejected,
            "duplicates": self._duplicates,
            "refused_packets": self._refused_packets,
            "durable": _durable_store() is not None,
            "queue_sizes": {t: q.qsize() for t, q in self._queues.items()},
        }


def build_graph_inference_result_envelope(
    *,
    tenant_id: str,
    inference_outputs: list[dict[str, Any]],
) -> TransportPacket:
    """
    Factory to build a valid graph inference TransportPacket.
    """
    return create_transport_packet(
        action="graph-inference-result",
        payload={"inference_outputs": inference_outputs},
        tenant=tenant_id,
        source_node="graph-service",
        destination_node="gate",
        reply_to="graph-service",
        classification="internal",
        compliance_tags=("GRAPH_INFERENCE",),
    )


async def handle_graph_inference_result(
    tenant: str,
    payload: dict[str, Any],
    packet: TransportPacket,
) -> dict[str, Any]:
    """
    Runtime handler for action=graph-inference-result.
    """
    channel = GraphReturnChannel.get_instance()
    count = await channel.submit(packet)

    return {
        "status": "accepted",
        "targets_queued": count,
        "packet_id": str(packet.header.packet_id),
        "tenant": tenant,
    }
