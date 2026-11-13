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


FORGERIES: dict[str, dict[str, Any]] = {
    "non_gate_source": {"source_node": "odoo"},
    "wrong_destination": {"destination_node": "graph"},
    "not_resolved_by_gate": {"resolved_by_gate": False},
    # Impersonates Gate in every field, but carries no Gate signature.
    "unsigned_gate_impersonation": {},
}


def _forged(name: str) -> TransportPacket:
    """A packet shaped exactly like Gate's dispatch to EIE, minus Gate's authorship."""
    shape = {"source_node": "gate", "destination_node": "enrichment-engine"} | FORGERIES[name]
    return create_transport_packet(
        action="converge",
        payload=odoo_request(f"e2e:forged:{name}"),
        tenant=TENANT,
        source_node=shape["source_node"],
        destination_node=shape["destination_node"],
        reply_to="gate",
        provenance=RoutingProvenance(
            origin_kind="node",
            requested_action="converge",
            resolved_by_gate=shape.get("resolved_by_gate", True),
            route_kind="external_ingress",
            original_source_node="odoo",
        ),
    )


@pytest.mark.parametrize("name", sorted(FORGERIES))
def test_forged_packet_posted_directly_to_eie_is_rejected(name: str) -> None:
    command = ["docker", "exec", "-i", EIE_CONTAINER, "python", "-c", _POST_FROM_EIE]
    body = _forged(name).model_dump_json().encode()
    done = subprocess.run(command, input=body, capture_output=True, check=True, timeout=90)
    status, _, response = done.stdout.decode().partition("\n")
    # The SDK runtime owns /v1/execute and refuses a packet either with a 4xx or
    # with a `failure` packet; what must never come back is a `response`, which
    # means the handler ran for a caller that is not Gate.
    refused = 400 <= int(status) < 500 or json.loads(response)["header"]["packet_type"] == "failure"
    assert refused, f"EIE executed a forged packet (HTTP {status}): {response[:600]}"
