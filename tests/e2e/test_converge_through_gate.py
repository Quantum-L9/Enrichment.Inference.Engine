"""Odoo `converge` through a real Gate: client -> Gate -> EIE -> Gate -> client.

Runs against the Docker rail started by `run_converge_e2e.sh` (see README.md).
The client is the installed Gate_SDK `GateClient`, signing as the admitted
consumer `odoo-e2e` and verifying Gate's signature on the reply. The request is
the shape IB-Odoo_19 `build_converge_request` emits: `contracts/converge_request.json`
plus the `kb_context`, `schema` and `idempotency_key` the builder adds.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from constellation_node_sdk import sign_transport_packet
from constellation_node_sdk.gate import GateClient, GateClientConfig
from constellation_node_sdk.transport.packet import TransportPacket, create_transport_packet
from constellation_node_sdk.transport.provenance import RoutingProvenance

REPO_ROOT = Path(__file__).resolve().parents[2]
TENANT = "plasticos"
BUDGET_SECONDS = 25.0
EIE_CONTAINER = os.environ.get("EIE_E2E_EIE_CONTAINER", "l9e2e-eie")
# IB-Odoo_19 plasticos_gate/services/gate_allowlists.py PARTNER_WRITEBACK_FIELD_ALLOWLIST.
PARTNER_WRITEBACK_FIELDS = ("city", "email", "name", "phone", "street", "street2", "website", "zip")
# Posts stdin to EIE's own /v1/execute from inside EIE's container, i.e. from
# EIE's network without passing through Gate. Prints "<status>\n<body>".
_POST_FROM_EIE = (
    "import sys,urllib.request,urllib.error\n"
    "req=urllib.request.Request('http://127.0.0.1:8000/v1/execute',data=sys.stdin.buffer.read(),"
    "headers={'Content-Type':'application/json'})\n"
    "try:\n r=urllib.request.urlopen(req,timeout=60)\nexcept urllib.error.HTTPError as e:\n r=e\n"
    "sys.stdout.write(str(r.status if hasattr(r,'status') else r.code)+'\\n'+r.read().decode())\n"
)


def odoo_request(idempotency_key: str | None = None) -> dict[str, Any]:
    payload = json.loads((REPO_ROOT / "contracts" / "converge_request.json").read_text("utf-8"))
    payload.pop("_comment")
    odoo = payload["odoo"]
    payload["kb_context"] = payload["object_type"]
    payload["schema"] = dict.fromkeys(PARTNER_WRITEBACK_FIELDS, "string")
    payload["idempotency_key"] = idempotency_key or (
        f"odoo:enrichment:{odoo['db_name']}:{odoo['model']}:{odoo['record_id']}"
    )
    return payload


def _rail_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in Path(os.environ["EIE_E2E_ENV_FILE"]).read_text("utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            env[key] = value.strip("'")
    return env


def converge(payload: dict[str, Any]) -> tuple[TransportPacket, float]:
    """One Odoo-shaped `converge` through Gate; returns Gate's reply and wall-clock seconds."""
    env = _rail_env()
    client = GateClient(
        GateClientConfig(
            gate_url=os.environ.get("EIE_E2E_GATE_URL", "http://127.0.0.1:19000"),
            local_node="odoo",
            require_signature=True,
            signing_key=env["L9E2E_ODOO_KEY"],
            signing_key_id="odoo-e2e",
            signing_algorithm="hmac-sha256",
            verify_response_signatures=True,
            verifying_keys={"gate-e2e": env["L9E2E_GATE_KEY"]},
        )
    )
    started = time.monotonic()
    packet = asyncio.run(
        client.execute(
            action="converge",
            payload=payload,
            tenant=dict.fromkeys(("actor", "on_behalf_of", "org_id"), TENANT)
            | {"originator": "odoo"},
            correlation_id=payload["odoo"]["correlation_id"],
            compliance_tags=("ERP", "ENRICHMENT"),
            idempotency_key=payload["idempotency_key"],
            timeout_ms=int(BUDGET_SECONDS * 1000),
        )
    )
    return packet, time.monotonic() - started


RoundTrip = tuple[dict[str, Any], TransportPacket, float]


@pytest.fixture(scope="module")
def round_trip() -> RoundTrip:
    request = odoo_request()
    packet, elapsed = converge(request)
    return request, packet, elapsed


def test_reply_is_gate_signed_response(round_trip: RoundTrip) -> None:
    _, packet, _ = round_trip
    assert packet.header.packet_type == "response"
    assert packet.security.signing_key_id == "gate-e2e"


def test_state_is_completed(round_trip: RoundTrip) -> None:
    _, packet, _ = round_trip
    assert packet.payload.get("state") == "completed", packet.payload


def test_fields_are_a_subset_of_the_requested_schema(round_trip: RoundTrip) -> None:
    request, packet, _ = round_trip
    fields = packet.payload.get("fields")
    assert isinstance(fields, dict), packet.payload
    assert set(fields) <= set(request["schema"]), sorted(set(fields) - set(request["schema"]))


def test_round_trip_under_25_seconds(round_trip: RoundTrip) -> None:
    _, _, elapsed = round_trip
    assert elapsed < BUDGET_SECONDS, f"round trip took {elapsed:.2f}s"


def test_identical_replay_returns_identical_business_result(round_trip: RoundTrip) -> None:
    request, packet, _ = round_trip
    replay, _ = converge(request)
    assert replay.payload == packet.payload


# Each forgery is Gate's own dispatch shape with exactly one thing wrong, and
# names the refusal EIE's boundary must give for it (app/middleware/gate_ingress.py).
# The first three are signed under Gate's key id so the key binding passes and
# the named claim check is the one that answers; EIE never verifies that
# signature because the boundary refuses first.
FORGERIES: dict[str, tuple[dict[str, Any], str]] = {
    "non_gate_origin": ({"origin_kind": "node"}, "origin_kind must be 'gate'"),
    "non_gate_source": ({"source_node": "odoo"}, "must originate from 'gate'"),
    "wrong_destination": ({"destination_node": "graph"}, "does not match local node"),
    "not_resolved_by_gate": ({"resolved_by_gate": False}, "resolved_by_gate must be true"),
    # Impersonates Gate in every claim, but carries no Gate signature.
    "unsigned_gate_impersonation": ({"unsigned": True}, "is not a Gate signing key"),
    # Every claim and the key id are Gate's; only the key is not. The boundary
    # cannot tell, so the SDK's signature verification must ("" = not a 403).
    "forged_signature_under_gate_key_id": ({}, ""),
}


def _forged(name: str) -> TransportPacket:
    """A packet shaped exactly like Gate's dispatch to EIE, minus Gate's authorship."""
    shape = {"source_node": "gate", "destination_node": "enrichment-engine"} | FORGERIES[name][0]
    packet = create_transport_packet(
        action="converge",
        payload=odoo_request(f"e2e:forged:{name}"),
        tenant=TENANT,
        source_node=shape["source_node"],
        destination_node=shape["destination_node"],
        reply_to="gate",
        provenance=RoutingProvenance(
            origin_kind=shape.get("origin_kind", "gate"),
            requested_action="converge",
            resolved_by_gate=shape.get("resolved_by_gate", True),
            route_kind="external_ingress",
            original_source_node="odoo",
        ),
    )
    if shape.get("unsigned"):
        return packet
    # A key the forger invented, presented under Gate's key id.
    return sign_transport_packet(
        packet, key="forged-key-not-gates" * 2, key_id="gate-e2e", algorithm="hmac-sha256"
    )


@pytest.mark.parametrize("name", sorted(FORGERIES))
def test_forged_packet_posted_directly_to_eie_is_rejected(name: str) -> None:
    command = ["docker", "exec", "-i", EIE_CONTAINER, "python", "-c", _POST_FROM_EIE]
    body = _forged(name).model_dump_json().encode()
    done = subprocess.run(command, input=body, capture_output=True, check=True, timeout=90)
    status, _, response = done.stdout.decode().partition("\n")
    # EIE's Gate-only boundary answers 403 before the SDK route or any handler
    # runs, and says which proof the packet lacked.
    expected = FORGERIES[name][1]
    if not expected:
        body = json.loads(response)
        executed = body.get("header", {}).get("packet_type") == "response"
        assert not executed, f"EIE executed a packet with a forged Gate signature: {response[:600]}"
        return
    assert int(status) == 403, f"forged packet was not refused (HTTP {status}): {response[:600]}"
    detail = json.loads(response)["detail"]
    assert detail.startswith("gate-only ingress: ") and expected in detail, detail
