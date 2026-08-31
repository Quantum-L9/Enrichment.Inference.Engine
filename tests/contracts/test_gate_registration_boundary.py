"""Why EIE still builds its own Gate registration body, and when it must stop.

ADR-EIE-013 makes Gate_SDK the owner of Gate transport, registration included:
EIE should supply registration *semantics* (node name, owner, actions, health
endpoint) and let the SDK own the HTTP. EIE does not do that today, and the
reason is a concrete, checkable capability gap rather than a preference — so it
is checked here rather than asserted in a comment that will rot.

The gap: Constellation.Gate's `action_ownership.assert_can_claim` refuses a
canonical action unless the registration carries `metadata.owner`, and
`constellation_node_sdk.gate.registration.build_registration_payload` emits
only `version`, `type` and `generated_by`. Routing EIE's registration through
the SDK as it stands would therefore have Gate reject `converge` — the node
would start, report healthy, and never receive a packet.

These tests are a tripwire pointing at the migration, not a defence of the
bespoke client. `test_sdk_still_cannot_express_owner` FAILS once the SDK grows
`metadata.owner`, and that failure is the signal to delete
`app/services/gate_registration.py`'s HTTP and adopt the SDK path.

L9_META:
  tier: 2
  domain: transport
  authority: L9 Master Kernel v3.0
  pr_class: app_code + tier2_test
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services import gate_registration

pytestmark = pytest.mark.unit


def _spec() -> dict:
    """The SDK's own input shape, carrying everything EIE would want to say."""
    return {
        "node": {
            "id": gate_registration.NODE_NAME,
            "actions": list(gate_registration.SUPPORTED_ACTIONS),
            "internal_url": "http://enrichment-engine:8000",
            "health_endpoint": gate_registration.HEALTH_ENDPOINT,
            "version": gate_registration.NODE_VERSION,
            "type": "enrichment",
            "owner": "eie",
        }
    }


def test_sdk_still_cannot_express_owner() -> None:
    """The blocker, stated executably.

    When this fails, the SDK has gained the capability: migrate EIE onto
    `constellation_node_sdk.gate.registration` and delete the local httpx call.
    """
    from constellation_node_sdk.gate.registration import build_registration_payload

    payload = build_registration_payload(_spec())
    metadata = payload[gate_registration.NODE_NAME]["metadata"]

    assert "owner" not in metadata, (
        "Gate_SDK now emits metadata.owner — the reason EIE builds its own "
        "registration body is gone. Migrate to the SDK path (ADR-EIE-013) and "
        "delete this test."
    )


def test_eie_payload_carries_what_gate_requires() -> None:
    """Whatever builds it, the body must satisfy Gate's ownership check."""
    settings = Settings(gate_url="http://gate:8080")
    payload = gate_registration.build_payload(settings)

    node = payload[gate_registration.NODE_NAME]
    assert node["metadata"]["owner"] == "eie"
    assert "converge" in node["supported_actions"]
    # Gate appends /v1/execute to internal_url; the health path is EIE's own
    # and is NOT the SDK default of /v1/health.
    assert node["health_endpoint"] == "/api/v1/health"


def test_registration_actions_are_all_served_by_a_runtime_handler() -> None:
    """Do not advertise to Gate an action this node cannot answer.

    A registered action with no handler is a routing black hole: Gate resolves
    it here and the SDK raises "no handler registered" on arrival. Equally, an
    action the SDK runtime does not allow is rejected at validation before any
    handler is reached, so both surfaces have to agree with the registration.

    The registration surface is the SDK handler registry itself, not
    `get_handler_map()`: `enrich-and-sync` is registered by
    `orchestration_layer.register` and would be invisible to a handler-map
    check, which would report a false gap.
    """
    from unittest.mock import MagicMock

    from constellation_node_sdk.runtime.handlers import registered_actions

    from app.engines import orchestration_layer
    from app.main import _build_runtime_config
    from app.services import chassis_handlers

    orchestration_layer.register(kb=MagicMock(), idem_store=None, domain_reader=None)
    chassis_handlers.register_all_handlers()

    implemented = set(registered_actions())
    runtime_allowed = set(_build_runtime_config().allowed_actions)
    advertised = set(gate_registration.SUPPORTED_ACTIONS)

    assert advertised <= implemented, f"advertised but unimplemented: {advertised - implemented}"
    assert advertised <= runtime_allowed, (
        f"advertised to Gate but rejected by the SDK runtime: {advertised - runtime_allowed}"
    )
    assert implemented <= runtime_allowed, (
        f"handler registered but rejected by the SDK runtime: {implemented - runtime_allowed}"
    )


def test_registration_never_reports_success_without_a_gate() -> None:
    """Registration is non-fatal, but "not attempted" is not "registered"."""
    import asyncio

    settings = Settings(gate_url="")
    assert asyncio.run(gate_registration.register_with_gate(settings)) is None
