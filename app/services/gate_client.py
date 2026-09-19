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

import contextlib
import os
import threading
from collections.abc import Iterator

from constellation_node_sdk.gate import (
    GateClient,
    GateClientConfig,
    get_gate_client_config_from_env,
)

# Serialises the scoped GATE_URL window below. os.environ is process-global, so
# two threads building clients for different Gates could otherwise observe each
# other's value inside the window.
_ENV_WINDOW_LOCK = threading.Lock()


@contextlib.contextmanager
def _gate_url_visible_to_sdk(url: str) -> Iterator[None]:
    """Make GATE_URL readable by the SDK factory for this call, and no longer.

    ``get_gate_client_config_from_env`` raises ``ValueError("GATE_URL is
    required")`` before reading anything else, so the variable has to be present
    for the duration of that call. It must not be present afterwards:
    EIE-005 — this module used to write ``os.environ["GATE_URL"]`` and leave it
    there, so the first client ever constructed pinned GATE_URL for the whole
    process. A later caller asking for a different Gate silently got the first
    one, and nothing reading configuration could see why.

    Restores the previous state exactly, including absence.
    """
    previous = os.environ.get("GATE_URL")
    already_set = bool((previous or "").strip())
    with _ENV_WINDOW_LOCK:
        if already_set:
            # The operator exported a value; touch nothing and restore nothing.
            yield
            return

        os.environ["GATE_URL"] = url
        try:
            yield
        finally:
            # No `return` in this block: it would discard an exception raised
            # inside the window (ruff B012) — including the SDK's own
            # ValueError, which the caller must see.
            if previous is None:
                os.environ.pop("GATE_URL", None)
            else:
                os.environ["GATE_URL"] = previous


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

    # Settings may load GATE_URL / L9_SIGNING_* from .env into process config
    # without exporting them to os.environ. Show GATE_URL to the SDK factory for
    # the length of that call only, so it sees the same plane as Settings and
    # still picks up sibling L9_SIGNING_* keys that *are* already exported —
    # without leaving a process-wide pin behind (EIE-005).
    with _gate_url_visible_to_sdk(normalized_url):
        base = get_gate_client_config_from_env()

    # The caller's URL is authoritative regardless of what the environment held.
    return GateClientConfig(
        **{
            **base.model_dump(),
            "gate_url": normalized_url,
            "local_node": EIE_NODE_NAME,
            "timeout_seconds": float(timeout_seconds),
        }
    )


def build_gate_client(gate_url: str, *, timeout_seconds: float) -> GateClient:
    """Construct the Gate-only client EIE uses for every outbound packet."""
    return GateClient(build_gate_client_config(gate_url, timeout_seconds=timeout_seconds))


__all__ = ["EIE_NODE_NAME", "build_gate_client", "build_gate_client_config"]
