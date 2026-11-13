"""Gate-only ingress boundary for the SDK-owned ``POST /v1/execute`` (C-21, GATE-KEY-01).

Runs in front of the SDK route and never dispatches. The SDK's ``inbound_policy``
owns source, destination and Gate-resolution; this adds what it lacks: it accepts
``origin_kind`` client/node/gate and does not bind the signing key id, so a peer
whose key this node verifies could claim Gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import structlog
from constellation_node_sdk import NodeRuntimeConfig
from constellation_node_sdk.runtime.inbound_policy import validate_execute_ingress_packet
from constellation_node_sdk.transport.errors import TransportError
from constellation_node_sdk.transport.packet import TransportPacket
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = structlog.get_logger("gate_ingress")

EXECUTE_PATH = "/v1/execute"
TRUST_REQUIRED_ENVIRONMENTS = frozenset({"staging", "prod"})
SYMMETRIC_ALGORITHM = "hmac-sha256"


@dataclass(frozen=True)
class GateIngressPolicy:
    """What a packet must prove to cross the ``/v1/execute`` boundary."""

    local_node: str
    gate_node: str
    require_route_kind: bool = True
    gate_key_ids: frozenset[str] | None = None
    refuse_symmetric: bool = False


def gate_ingress_violation(packet: TransportPacket, policy: GateIngressPolicy) -> str | None:
    """Return why ``packet`` is not a Gate-authored packet for this node, else None."""
    if policy.gate_key_ids is not None:
        key_id = (packet.security.signing_key_id or "").strip()
        if key_id not in policy.gate_key_ids:
            return f"security.signing_key_id {key_id!r} is not a Gate signing key"
        algorithm = (packet.security.signature_algorithm or "").strip().lower()
        if policy.refuse_symmetric and algorithm == SYMMETRIC_ALGORITHM:
            return f"{SYMMETRIC_ALGORITHM} cannot prove Gate authorship here; Gate must sign with ed25519"
    origin_kind = packet.provenance.origin_kind
    if origin_kind != "gate":
        return f"provenance.origin_kind must be 'gate', got {origin_kind!r}"
    try:
        validate_execute_ingress_packet(
            packet,
            local_node=policy.local_node,
            gate_node_name=policy.gate_node,
            require_route_kind=policy.require_route_kind,
        )
    except ValueError as exc:
        return str(exc)
    return None


def resolve_gate_key_binding(
    environment: str, raw_key_ids: str
) -> tuple[frozenset[str] | None, bool]:
    """GATE-KEY-01: the Gate key ids ``/v1/execute`` accepts, and the HMAC refusal.

    In staging/prod ``L9_GATE_SIGNING_KEY_IDS`` is required (startup fails
    without it) and hmac-sha256 is refused: every verifier of a shared HMAC key
    can mint it. Elsewhere an unset list leaves the claim checks only.
    """
    key_ids = frozenset(part.strip() for part in raw_key_ids.split(",") if part.strip())
    trust_required = environment in TRUST_REQUIRED_ENVIRONMENTS
    if key_ids:
        return key_ids, trust_required
    if trust_required:
        raise RuntimeError(
            f"L9_GATE_SIGNING_KEY_IDS is required when L9_ENVIRONMENT={environment} "
            "(GATE-KEY-01): without it any peer whose key this node verifies can "
            "claim Gate authorship"
        )
    logger.warning("gate_signing_key_binding_unset", environment=environment)
    return None, False


class GateOnlyIngressMiddleware:
    """Answer 403 to a ``POST /v1/execute`` packet that violates the policy."""

    def __init__(self, app: ASGIApp, *, policy: GateIngressPolicy) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or (scope["method"], scope["path"]) != ("POST", EXECUTE_PATH):
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return  # client disconnected before the body arrived
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        raw = b"".join(chunks)

        reason = self._violation(raw)
        if reason is not None:
            logger.warning("gate_only_ingress_rejected", reason=reason)
            detail = {"detail": f"gate-only ingress: {reason}"}
            await JSONResponse(status_code=403, content=detail)(scope, receive, send)
            return

        replayed = False

        async def replay() -> Message:
            """Hand the buffered body to the SDK route once, then defer to the client."""
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": raw, "more_body": False}

        await self.app(scope, replay, send)

    def _violation(self, raw: bytes) -> str | None:
        try:
            packet = TransportPacket.model_validate(json.loads(raw))
        except (ValueError, ValidationError, TransportError):
            # Malformed JSON and non-packet bodies are the SDK route's to refuse.
            return None
        return gate_ingress_violation(packet, self.policy)


def install_gate_only_ingress(
    app: Starlette, *, config: NodeRuntimeConfig, gate_signing_key_ids: str
) -> bool:
    """Put the boundary in front of the SDK route; honors ``L9_ENFORCE_GATE_ONLY_INGRESS``."""
    if not config.enforce_gate_only_ingress:
        logger.warning("gate_only_ingress_disabled", node=config.node_name)
        return False
    gate_key_ids, refuse_symmetric = resolve_gate_key_binding(
        config.environment, gate_signing_key_ids
    )
    policy = GateIngressPolicy(
        local_node=config.node_name,
        gate_node=config.gate_node_name,
        require_route_kind=config.require_gate_mediation_provenance,
        gate_key_ids=gate_key_ids,
        refuse_symmetric=refuse_symmetric,
    )
    app.add_middleware(GateOnlyIngressMiddleware, policy=policy)
    logger.info("gate_only_ingress_enforced", key_binding=gate_key_ids is not None)
    return True
