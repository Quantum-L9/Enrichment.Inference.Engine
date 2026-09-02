"""
app/services/gate_client.py — the one place EIE builds a Gate_SDK client.

Every outbound collaborative message EIE sends (graph sync, match, outcomes,
score invalidation) leaves through ``constellation_node_sdk.GateClient`` and is
addressed to Gate only. Before this module existed, three call sites each
constructed their own ``GateClientConfig(gate_url, local_node, timeout)`` and
none of them carried signing material — so in an environment where Gate
requires signatures (``L9_REQUIRE_SIGNATURE=true``) every EIE-originated packet
was rejected at Gate ingress while unit tests stayed green (seam audit
2026-09-02, finding EIE-AUTH-01).

Configuration authority is the SDK's own environment contract
(``get_gate_client_config_from_env``: ``GATE_URL``, ``L9_SIGNING_KEY``,
``L9_SIGNING_KEY_ID``, ``L9_SIGNING_ALGORITHM``, ``L9_REQUIRE_SIGNATURE``,
``GATE_CLIENT_TIMEOUT_SECONDS`` ...). EIE only pins the two values it owns:
its node identity and the per-call operation budget. No second deadline, no
second retry plane, no peer URL.
"""

from __future__ import annotations

import os

from constellation_node_sdk.gate import (
    GateClient,
    GateClientConfig,
    get_gate_client_config_from_env,
)

# EIE's runtime node identity. It MUST match the name EIE registers with Gate
# (app/main.py NODE_NAME) and the destination Gate dispatches to; the SDK's
# outbound policy also requires packet.address.source_node == local_node.
EIE_NODE_NAME = "enrichment-engine"


def build_gate_client_config(
    gate_url: str,
    *,
    timeout_seconds: float,
) -> GateClientConfig:
    """Build the canonical EIE -> Gate client configuration.

    ``gate_url`` is EIE's configured Gate base URL (settings.gate_url, itself
    read from ``GATE_URL``). Signing and verification material comes from the
    SDK environment contract when ``GATE_URL`` is set in the process
    environment; otherwise (tests, ad-hoc tooling) the client is built
    unsigned from the URL alone.
    """
    normalized_url = gate_url.strip().rstrip("/")
    if not normalized_url:
        raise ValueError("gate_url must be configured for Gate-only egress")

    if os.getenv("GATE_URL", "").strip():
        # Read via the SDK contract, then pin EIE's identity and this call's
        # operation budget. `GATE_URL` in the environment and settings.gate_url
        # are the same variable; the explicit argument wins so a caller
        # constructed from Settings never diverges from what it was given.
        base = get_gate_client_config_from_env()
        return GateClientConfig(
            **{
                **base.model_dump(),
                "gate_url": normalized_url,
                "local_node": EIE_NODE_NAME,
                "timeout_seconds": float(timeout_seconds),
            }
        )

    return GateClientConfig(
        gate_url=normalized_url,
        local_node=EIE_NODE_NAME,
        timeout_seconds=float(timeout_seconds),
    )


def build_gate_client(gate_url: str, *, timeout_seconds: float) -> GateClient:
    """Construct the Gate-only client EIE uses for every outbound packet."""
    return GateClient(build_gate_client_config(gate_url, timeout_seconds=timeout_seconds))


__all__ = ["EIE_NODE_NAME", "build_gate_client", "build_gate_client_config"]
