"""Gate-only ingress on the SDK-owned POST /v1/execute (C-21, GATE-KEY-01).

One negative test per rejection, a positive test that a Gate-shaped packet reaches
the SDK route with its body intact, and the C-21 ownership check on the live app.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from constellation_node_sdk import NodeRuntimeConfig, create_node_app, sign_transport_packet
from constellation_node_sdk.runtime.handlers import clear_handlers, register_handler
from constellation_node_sdk.transport import create_transport_packet
from constellation_node_sdk.transport.provenance import RoutingProvenance
from fastapi.testclient import TestClient

from app.middleware.gate_ingress import (
    GateOnlyIngressMiddleware,
    install_gate_only_ingress,
    resolve_gate_key_binding,
)

NODE = "enrichment-engine"
PAYLOAD = {"entity_id": "e-001", "fields": {"name": "Alpha Recyclers"}}


def _body(
    *,
    origin_kind: str = "gate",
    resolved_by_gate: bool = True,
    source_node: str = "gate",
    destination_node: str = NODE,
    sign_as: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """A packet shaped like Gate's dispatch, with one field optionally wrong."""
    packet = create_transport_packet(
        action="enrich",
        payload=PAYLOAD,
        tenant="acme",
        source_node=source_node,
        destination_node=destination_node,
        reply_to="gate",
        provenance=RoutingProvenance(
            origin_kind=origin_kind,
            requested_action="enrich",
            resolved_by_gate=resolved_by_gate,
            route_kind="external_ingress",
            original_source_node="client",
        ),
    )
    if sign_as is not None:
        key_id, algorithm = sign_as
        packet = sign_transport_packet(packet, key="k" * 32, key_id=key_id, algorithm=algorithm)
    return packet.model_dump_json_dict()


def _client(*, key_ids: str = "", environment: str = "test", **overrides: Any) -> TestClient:
    config = NodeRuntimeConfig(
        environment=environment,
        node_name=NODE,
        service_name=NODE,
        service_version="test",
        dev_mode=environment == "test",
        allowed_actions=("enrich",),
        **overrides,
    )
    app = create_node_app(config=config, auto_register_with_gate=False)
    install_gate_only_ingress(app, config=config, gate_signing_key_ids=key_ids)
    return TestClient(app)


@pytest.fixture
def seen() -> Iterator[list[dict[str, Any]]]:
    """Register an `enrich` handler that records what the SDK route delivered."""
    calls: list[dict[str, Any]] = []

    async def handle(tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append({"tenant": tenant, "payload": payload})
        return {"status": "completed"}

    clear_handlers()
    register_handler("enrich", handle)
    yield calls
    clear_handlers()


def _rejected(response: Any, fragment: str, seen: list[dict[str, Any]]) -> None:
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail.startswith("gate-only ingress: ") and fragment in detail, detail
    assert seen == [], "a refused packet must never reach a handler"


def test_gate_shaped_packet_reaches_the_sdk_route_intact(seen: list[dict[str, Any]]) -> None:
    response = _client().post("/v1/execute", json=_body())
    assert response.status_code == 200, response.text
    assert seen == [{"tenant": "acme", "payload": PAYLOAD}]
    assert response.json()["address"]["source_node"] == NODE


def test_rejects_packet_not_authored_by_gate(seen: list[dict[str, Any]]) -> None:
    response = _client().post("/v1/execute", json=_body(origin_kind="node"))
    _rejected(response, "provenance.origin_kind must be 'gate', got 'node'", seen)


def test_rejects_packet_not_resolved_by_gate(seen: list[dict[str, Any]]) -> None:
    response = _client().post("/v1/execute", json=_body(resolved_by_gate=False))
    _rejected(response, "provenance.resolved_by_gate must be true", seen)


def test_rejects_packet_whose_source_is_not_gate(seen: list[dict[str, Any]]) -> None:
    response = _client().post("/v1/execute", json=_body(source_node="graph"))
    _rejected(response, "must originate from 'gate', got 'graph'", seen)


def test_rejects_packet_targeted_at_another_node(seen: list[dict[str, Any]]) -> None:
    response = _client().post("/v1/execute", json=_body(destination_node="graph"))
    _rejected(response, "destination_node 'graph' does not match local node", seen)


def test_rejects_key_id_that_is_not_a_gate_key(seen: list[dict[str, Any]]) -> None:
    client = _client(key_ids="gate-v1, gate-v2")
    peer = client.post("/v1/execute", json=_body(sign_as=("graph-engine-v1", "hmac-sha256")))
    _rejected(peer, "'graph-engine-v1' is not a Gate signing key", seen)
    _rejected(client.post("/v1/execute", json=_body()), "'' is not a Gate signing key", seen)
    # Outside staging/prod an hmac signature under a Gate key id still passes.
    assert client.post("/v1/execute", json=_body(sign_as=("gate-v1", "hmac-sha256"))).is_success
    assert len(seen) == 1


def test_rejects_hmac_where_trust_is_required(seen: list[dict[str, Any]]) -> None:
    client = _client(
        key_ids="gate-v1",
        environment="staging",
        require_signature=True,
        signing_key="k" * 32,
        signing_key_id="enrichment-engine-test",
        verifying_keys={"gate-v1": "k" * 32},
    )
    response = client.post("/v1/execute", json=_body(sign_as=("gate-v1", "hmac-sha256")))
    _rejected(response, "hmac-sha256 cannot prove Gate authorship", seen)


@pytest.mark.parametrize("environment", ["local", "test", "dev"])
def test_key_binding_stays_optional_outside_staging_and_prod(environment: str) -> None:
    assert resolve_gate_key_binding(environment, "") == (None, False)
    assert resolve_gate_key_binding(environment, "gate-v1") == (frozenset({"gate-v1"}), False)


@pytest.mark.parametrize("environment", ["staging", "prod"])
def test_configured_gate_key_ids_bind_where_trust_is_required(environment: str) -> None:
    assert resolve_gate_key_binding(environment, "gate-v1, gate-v2") == (
        frozenset({"gate-v1", "gate-v2"}),
        True,
    )


@pytest.mark.parametrize("raw_key_ids", ["", " ", " , "])
@pytest.mark.parametrize("environment", ["staging", "prod"])
def test_missing_gate_key_ids_refuse_startup_before_serving(
    environment: str, raw_key_ids: str
) -> None:
    """GATE-KEY-01: staging/prod raise before middleware is installed or the lifespan runs."""
    config = NodeRuntimeConfig(
        environment=environment,
        node_name=NODE,
        service_name=NODE,
        service_version="test",
        dev_mode=False,
        allowed_actions=("enrich",),
    )
    app = create_node_app(config=config, auto_register_with_gate=False)
    with pytest.raises(RuntimeError, match="L9_GATE_SIGNING_KEY_IDS"):
        install_gate_only_ingress(app, config=config, gate_signing_key_ids=raw_key_ids)
    assert not any(m.cls is GateOnlyIngressMiddleware for m in app.user_middleware)
    # Construction leaves the runtime stopped. The lifespan that would accept
    # connections never runs, so it never flips lifecycle_started on.
    assert app.state.lifecycle_started is False
    assert app.state.runtime_ready is False


@pytest.mark.parametrize("environment", ["staging", "prod"])
def test_disabled_gate_only_ingress_refuses_startup_before_serving(environment: str) -> None:
    """Policy requires the boundary in staging/prod; a disabled flag must not reach serving."""
    config = NodeRuntimeConfig(
        environment=environment,
        node_name=NODE,
        service_name=NODE,
        service_version="test",
        dev_mode=False,
        allowed_actions=("enrich",),
        enforce_gate_only_ingress=False,
    )
    app = create_node_app(config=config, auto_register_with_gate=False)
    with pytest.raises(RuntimeError, match="L9_ENFORCE_GATE_ONLY_INGRESS"):
        install_gate_only_ingress(app, config=config, gate_signing_key_ids="gate-v1")
    assert not any(m.cls is GateOnlyIngressMiddleware for m in app.user_middleware)
    assert app.state.lifecycle_started is False
    assert app.state.runtime_ready is False


def _import_app_main(
    environment: str, *, key_ids: str, enforce: str
) -> subprocess.CompletedProcess[str]:
    """Import the live ASGI module the way uvicorn does, without starting a server."""
    env = os.environ.copy()
    env["L9_ENVIRONMENT"] = environment
    env["L9_GATE_SIGNING_KEY_IDS"] = key_ids
    env["L9_ENFORCE_GATE_ONLY_INGRESS"] = enforce
    env["L9_DEV_MODE"] = "false"
    return subprocess.run(
        [sys.executable, "-c", "import app.main"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.parametrize("environment", ["staging", "prod"])
def test_live_app_import_refuses_missing_key_ids_before_serving(environment: str) -> None:
    result = _import_app_main(environment, key_ids="", enforce="true")
    assert result.returncode != 0
    assert "L9_GATE_SIGNING_KEY_IDS" in result.stderr
    assert "Uvicorn running" not in result.stderr
    assert "Uvicorn running" not in result.stdout


@pytest.mark.parametrize("environment", ["staging", "prod"])
def test_live_app_import_refuses_disabled_gate_only_ingress_before_serving(
    environment: str,
) -> None:
    result = _import_app_main(environment, key_ids="gate-v1", enforce="false")
    assert result.returncode != 0
    assert "L9_ENFORCE_GATE_ONLY_INGRESS" in result.stderr
    assert "Uvicorn running" not in result.stderr


def test_live_app_import_still_loads_in_local_without_gate_key_ids() -> None:
    result = _import_app_main("local", key_ids="", enforce="true")
    assert result.returncode == 0, result.stderr
    assert "Uvicorn running" not in result.stdout


def test_malformed_bodies_and_other_routes_are_left_to_the_sdk(seen: list[dict[str, Any]]) -> None:
    client = _client()
    assert client.post("/v1/execute", content=b"{not json").status_code == 400
    assert client.post("/v1/execute", json=["not", "a", "packet"]).status_code != 403
    assert client.get("/v1/health").status_code == 200
    assert seen == []


def test_disabled_enforcement_installs_no_boundary(seen: list[dict[str, Any]]) -> None:
    client = _client(enforce_gate_only_ingress=False)
    assert client.post("/v1/execute", json=_body(origin_kind="node")).status_code == 200
    assert len(seen) == 1


def test_live_app_serves_execute_from_the_sdk_behind_the_boundary() -> None:
    """C-21: one /v1/execute route, the SDK's, with the boundary inside the rate limiter."""
    from app.main import app
    from app.middleware.rate_limiter import RateLimitMiddleware

    routes = [r for r in app.routes if getattr(r, "path", None) == "/v1/execute"]
    assert [r.endpoint.__module__ for r in routes] == ["constellation_node_sdk.runtime.app"]
    order = [m.cls for m in app.user_middleware]
    assert order.index(RateLimitMiddleware) < order.index(GateOnlyIngressMiddleware)
