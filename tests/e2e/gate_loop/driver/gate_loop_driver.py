#!/usr/bin/env python3
"""gate_loop scenario driver (TEST_ONLY). Runs INSIDE the Gate image on the
control network, where Gate is the only reachable service.

The driver uses the SDK pinned by Gate's lock: the real GateClient for
positive flows, and a real signed packet with exactly one property changed for
negative flows. A packet signed with a worker key here is a *key misassignment*
negative (P10). It is never presented as the worker's own egress; that
evidence comes from the worker processes (rail.py egress probes).

Usage: gate_loop_driver.py <scenario> [json-args]. One JSON document goes to
stdout. Secrets are never printed: key ids appear, key material never does.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from constellation_node_sdk.gate.client import GateClient
from constellation_node_sdk.gate.config import GateClientConfig
from constellation_node_sdk.security.signing import sign_transport_packet
from constellation_node_sdk.security.verification import verify_transport_packet_signature
from constellation_node_sdk.transport.packet import TransportPacket, create_transport_packet

GATE_URL = os.environ.get("GATE_URL", "http://gate:9000").rstrip("/")
PUB: dict[str, str] = json.loads(os.environ["GL_ALL_PUBLIC_KEYS_JSON"])
PRIV = {
    "odoo-gl": os.environ["GL_PRIV_ODOO"],
    "op-gl": os.environ["GL_PRIV_OP"],
    "rogue-gl": os.environ["GL_PRIV_ROGUE"],
    "eie-gl": os.environ["GL_PRIV_EIE"],
    "ceg-gl": os.environ["GL_PRIV_CEG"],
}
NODE_OF = {
    "odoo-gl": "odoo",
    "op-gl": "operator",
    "rogue-gl": "odoo",
    "eie-gl": "enrichment-engine",
    "ceg-gl": "graph",
}
SIGNING = os.environ.get("GL_SIGNING", "ed25519")
HMAC = json.loads(os.environ.get("GL_HMAC_ALL_JSON") or "{}")
ALG = "ed25519" if SIGNING == "ed25519" else "hmac-sha256"
if SIGNING == "hmac":  # diagnostic profile: symmetric secrets replace key pairs
    PRIV = {k: HMAC[k] for k in PRIV}
    PUB = {k: HMAC[k] for k in HMAC}
GATE_TRUST = {"gate-gl": PUB["gate-gl"]}


def client(key_id: str = "odoo-gl", timeout: float = 40.0) -> GateClient:
    return GateClient(
        GateClientConfig(
            gate_url=GATE_URL,
            local_node=NODE_OF[key_id],
            timeout_seconds=timeout,
            require_signature=True,
            signing_key=PRIV[key_id],
            signing_key_id=key_id,
            signing_algorithm=ALG,
            verify_response_signatures=True,
            # Own PUBLIC key included on purpose (deviation D-3): the pinned SDK
            # resolves the client's own key id to its private signing key
            # (gate/config.py resolve_verifying_key), so an ed25519 client fails
            # its own outbound validation without it (finding F-A7-02).
            verifying_keys={**GATE_TRUST, key_id: PUB[key_id]},
        )
    )


def describe(pkt: TransportPacket) -> dict[str, Any]:
    """Correlation facts only; never key material."""
    return {
        "packet_id": str(pkt.header.packet_id),
        "packet_type": pkt.header.packet_type,
        "action": pkt.header.action,
        "source_node": pkt.address.source_node,
        "destination_node": pkt.address.destination_node,
        "reply_to": pkt.address.reply_to,
        "tenant": pkt.tenant.org_id,
        "root_id": str(pkt.lineage.root_id),
        "parent_id": str(pkt.lineage.parent_id) if pkt.lineage.parent_id else None,
        "generation": pkt.lineage.generation,
        "causation_id": str(pkt.header.causation_id) if pkt.header.causation_id else None,
        "idempotency_key": pkt.header.idempotency_key,
        "signing_key_id": pkt.security.signing_key_id,
        "signature_algorithm": pkt.security.signature_algorithm,
        "hop_trace": [
            {
                "node": h.node,
                "direction": h.direction,
                "status": h.status,
                "target_node": h.target_node,
                "signed": bool(h.hop_signature),
            }
            for h in pkt.hop_trace
        ],
        "provenance": pkt.provenance.model_dump(mode="json") if pkt.provenance else None,
    }


def gate_signature_valid(pkt: TransportPacket) -> bool:
    try:
        return bool(verify_transport_packet_signature(pkt, key_resolver=GATE_TRUST))
    except Exception:
        return False


async def execute(
    action: str, payload: dict, tenant: str, key_id: str = "odoo-gl", **kw: Any
) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        resp = await client(key_id).execute(action=action, payload=payload, tenant=tenant, **kw)
        return {
            "ok": True,
            "elapsed_s": round(time.monotonic() - t0, 3),
            "response": describe(resp),
            "gate_signature_valid": gate_signature_valid(resp),
            "payload": resp.payload,
        }
    except Exception as exc:
        return {
            "ok": False,
            "elapsed_s": round(time.monotonic() - t0, 3),
            "error": f"{type(exc).__name__}: {exc}"[:600],
        }


def packet(
    action: str,
    payload: dict,
    tenant: str,
    *,
    source: str,
    key_id: str | None,
    algorithm: str | None = None,
    key: str | None = None,
    expires_at: datetime | None = None,
    idem: str | None = None,
) -> TransportPacket:
    kw: dict[str, Any] = {}
    if expires_at is not None:
        kw["expires_at"] = expires_at
    pkt = create_transport_packet(
        action=action,
        payload=payload,
        tenant=tenant,
        destination_node="gate",
        source_node=source,
        reply_to=source,
        idempotency_key=idem,
        **kw,
    )
    if key_id is None:
        return pkt
    return sign_transport_packet(
        pkt, key=key if key is not None else PRIV[key_id], key_id=key_id, algorithm=algorithm or ALG
    )


async def post(pkt_or_wire: Any) -> dict[str, Any]:
    wire = pkt_or_wire if isinstance(pkt_or_wire, dict) else pkt_or_wire.model_dump(mode="json")
    async with httpx.AsyncClient(timeout=40.0) as c:
        r = await c.post(
            f"{GATE_URL}/v1/execute",
            content=json.dumps(wire).encode(),
            headers={"Content-Type": "application/json"},
        )
    try:
        body = r.json()
    except Exception:
        body = r.text[:400]
    out: dict[str, Any] = {"http_status": r.status_code}
    if isinstance(body, dict) and "header" in body:
        try:
            p = TransportPacket.model_validate(body)
            out["response"] = describe(p)
            out["gate_signature_valid"] = gate_signature_valid(p)
            out["payload"] = p.payload
        except Exception as exc:
            out["response_parse_error"] = str(exc)[:300]
    else:
        out["body"] = body
    return out


# ───────────────────────────── scenarios ─────────────────────────────


async def s_execute(args: dict) -> dict:
    return await execute(
        args["action"],
        args["payload"],
        args["tenant"],
        args.get("key_id", "odoo-gl"),
        **{
            k: v
            for k, v in args.items()
            if k in ("idempotency_key", "timeout_ms", "correlation_id")
        },
    )


async def s_policy_matrix(args: dict) -> dict:
    """A7.2 / P10 / P11: one signed packet per (key, action, tenant) cell, through Gate."""
    tenant = args.get("tenant", "plasticos")
    hm = {"subaction": "health_report"}
    probe_payloads = {
        "score": {"query": {"polymer_type": "HDPE"}, "candidate_ids": ["tp-fac-001"]},
        "health-assess": hm,
        "graph-inference-result": {"inference_outputs": []},
        "sync": {
            "entity_type": "facilities",
            "batch": [{"facility_id": "tp-policy-probe", "name": "policy probe"}],
        },
        "match": {
            "query": {"polymer_type": "PET", "contamination_pct": 1.0},
            "match_direction": "supply_opportunity_to_buyer_facility",
            "top_n": 1,
        },
    }
    cells = []
    for key_id in ("odoo-gl", "op-gl", "eie-gl", "ceg-gl"):
        for action in ("score", "health-assess", "graph-inference-result", "sync", "match"):
            pkt = packet(
                action, probe_payloads[action], tenant, source=NODE_OF[key_id], key_id=key_id
            )
            r = await post(pkt)
            cells.append(
                {
                    "key_id": key_id,
                    "source_node": NODE_OF[key_id],
                    "action": action,
                    "tenant": tenant,
                    "http_status": r["http_status"],
                    "body": r.get("body"),
                    "reached_worker": "response" in r,
                    "response_packet_type": (r.get("response") or {}).get("packet_type"),
                    "response_source": (r.get("response") or {}).get("source_node"),
                }
            )
    return {"cells": cells}


async def s_negatives(args: dict) -> dict:
    """P11/P12/P13: each case changes one property of a valid consumer packet."""
    tenant = "plasticos"
    good = {
        "query": {"polymer_type": "PET", "contamination_pct": 1.0},
        "match_direction": "supply_opportunity_to_buyer_facility",
        "top_n": 1,
    }
    res: dict[str, Any] = {}
    res["baseline_valid"] = await post(
        packet("match", good, tenant, source="odoo", key_id="odoo-gl")
    )
    res["unknown_action"] = await post(
        packet("teleport", {}, tenant, source="odoo", key_id="odoo-gl")
    )
    res["wrong_tenant"] = await post(
        packet("match", good, "othertenant", source="odoo", key_id="odoo-gl")
    )
    res["unsigned"] = await post(packet("match", good, tenant, source="odoo", key_id=None))
    res["unknown_key_rogue"] = await post(
        packet("match", good, tenant, source="odoo", key_id="rogue-gl")
    )
    forged = packet("match", good, tenant, source="odoo", key_id="odoo-gl").model_dump(mode="json")
    sig = forged["security"]["signature"]
    forged["security"]["signature"] = ("0" if sig[0] != "0" else "1") + sig[1:]
    res["corrupted_signature"] = await post(forged)
    res["key_claims_other_node"] = await post(
        packet("match", good, tenant, source="enrichment-engine", key_id="odoo-gl")
    )
    res["consumer_claims_registered_node_name"] = await post(
        packet("match", good, tenant, source="graph", key_id="odoo-gl")
    )
    # Algorithm confusion: HMAC over transport_hash keyed with the consumer's
    # PUBLIC key bytes, declared as hmac-sha256 under the ed25519 key id.
    res["hmac_downgrade_with_public_key"] = await post(
        packet(
            "match",
            good,
            tenant,
            source="odoo",
            key_id="odoo-gl",
            algorithm="hmac-sha256",
            key=PUB["odoo-gl"],
        )
    )
    res["hmac_downgrade_with_random_secret"] = await post(
        packet(
            "match",
            good,
            tenant,
            source="odoo",
            key_id="odoo-gl",
            algorithm="hmac-sha256",
            key=base64.b64encode(os.urandom(32)).decode(),
        )
    )
    # A fresh idempotency key per run, so a cached answer from an earlier run cannot satisfy P13.
    p = packet(
        "match",
        good,
        tenant,
        source="odoo",
        key_id="odoo-gl",
        idem=f"gl-neg-replay-{os.urandom(4).hex()}",
    )
    res["replay_packet_id"] = str(p.header.packet_id)
    res["replay_first"] = await post(p)
    res["replay_second_same_packet_id"] = await post(p)
    # A genuinely expired packet: signed with a 1 s deadline, posted after it.
    exp = packet(
        "match",
        good,
        tenant,
        source="odoo",
        key_id="odoo-gl",
        expires_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    await asyncio.sleep(2.5)
    res["expired_deadline"] = await post(exp)
    return res


async def s_isolation(args: dict) -> dict:
    out = {}
    for host in ("enrichment-engine", "graph", "neo4j", "eie-postgres", "gate"):
        try:
            out[host] = socket.gethostbyname(host)
        except OSError as exc:
            out[host] = f"unresolvable: {exc}"
    return out


async def s_post_signed(args: dict) -> dict:
    """One consumer-signed packet posted raw, returning its packet_id so the rail can
    correlate Gate's structured events (gate.failure / gate.completed) to this request."""
    key_id = args.get("key_id", "odoo-gl")
    pkt = packet(
        args["action"], args["payload"], args["tenant"], source=NODE_OF[key_id], key_id=key_id
    )
    out = await post(pkt)
    out["request_packet_id"] = str(pkt.header.packet_id)
    return out


SCENARIOS = {
    "execute": s_execute,
    "post_signed": s_post_signed,
    "policy_matrix": s_policy_matrix,
    "negatives": s_negatives,
    "isolation": s_isolation,
}

if __name__ == "__main__":
    name = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    print(json.dumps(asyncio.run(SCENARIOS[name](args)), default=str))
