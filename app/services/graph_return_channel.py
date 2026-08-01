"""
GRAPH -> ENRICH return channel for inference results.

Receives GRAPH inference outputs as `TransportPacket` instances and converts
them into `EnrichmentTarget` records for injection into the convergence loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
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


def validate_graph_inference_packet(packet: TransportPacket) -> None:
    """Validate graph inference packet semantics on top of SDK validation."""
    if packet.header.action != "graph-inference-result":
        msg = f"expected action='graph-inference-result', got {packet.header.action!r}"
        raise TransportValidationError(msg)

    outputs = packet.payload.get("inference_outputs")
    if not isinstance(outputs, list):
        raise TransportValidationError("'inference_outputs' must be a list")


def extract_targets_from_packet(packet: TransportPacket) -> list[EnrichmentTarget]:
    """Convert validated inference outputs to return-channel targets."""
    validate_graph_inference_packet(packet)

    targets: list[EnrichmentTarget] = []
    tenant_id = packet.tenant.org_id
    packet_id = str(packet.header.packet_id)
    for output in packet.payload.get("inference_outputs", []):
        confidence = safe_float(output.get("confidence", 0.0))
        if confidence < CONFIDENCE_FLOOR:
            logger.debug(
                "Skipping low-confidence inference output (%.3f < %.3f) for entity=%s field=%s",
                confidence,
                CONFIDENCE_FLOOR,
                output.get("entity_id"),
                output.get("field"),
            )
            continue

        targets.append(
            EnrichmentTarget(
                entity_id=str(output["entity_id"]),
                tenant_id=tenant_id,
                field_name=str(output["field"]),
                seed_value=output.get("value"),
                source_confidence=confidence,
                origin_packet_id=packet_id,
                origin_inference_rule=str(output.get("rule", "unknown")),
            )
        )
    return targets


class GraphReturnChannel:
    """
    Async queue bridging GRAPH inference outputs back to ENRICH convergence loop.

    Usage (SDK handler):
        channel = GraphReturnChannel.get_instance()
        await channel.submit(packet)

    Usage (convergence_controller):
        targets = await channel.drain(tenant_id="acme", timeout=0.5)
    """

    _instance: GraphReturnChannel | None = None

    def __init__(self, maxsize: int = 10_000) -> None:
        self._queues: dict[str, asyncio.Queue[EnrichmentTarget]] = {}
        self._maxsize = maxsize
        self._submitted: int = 0
        self._drained: int = 0
        self._rejected: int = 0

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
        Validate the packet, convert outputs to targets, enqueue them.
        Returns the number of targets enqueued.
        Raises TransportValidationError if the packet is invalid.
        """
        validate_graph_inference_packet(packet)
        targets = extract_targets_from_packet(packet)
        tenant_id = packet.tenant.org_id
        q = self._queue_for(tenant_id)
        count = 0
        for target in targets:
            try:
                q.put_nowait(target)
                count += 1
            except asyncio.QueueFull:
                self._rejected += 1
                logger.warning(
                    "EnrichmentTargetQueue full for tenant=%s — dropping target entity=%s field=%s",
                    tenant_id,
                    target.entity_id,
                    target.field_name,
                )
        self._submitted += count
        logger.info(
            "GraphReturnChannel: submitted %d targets for tenant=%s (packet=%s)",
            count,
            tenant_id,
            packet.header.packet_id,
        )
        return count

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
        if targets:
            logger.info(
                "GraphReturnChannel: drained %d targets for tenant=%s",
                len(targets),
                tenant_id,
            )
        return targets

    def stats(self) -> dict[str, Any]:
        return {
            "submitted": self._submitted,
            "drained": self._drained,
            "rejected": self._rejected,
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
