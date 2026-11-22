"""A4: graph-return intake binds, refuses, deduplicates and is consumed with evidence.

Contract: docs/contracts/data/graph_return_intake.md. The durable store is a
fake here (its SQL is pinned in test_graph_return_store_logic.py and run on
PostgreSQL in tests/integration/test_graph_return_store_pg.py); the channel,
the SDK error mapping and the converge consumption ledger are real.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from constellation_node_sdk.runtime.errors import classify_exception
from constellation_node_sdk.transport import create_transport_packet

from app.engines import convergence_controller as cc
from app.engines.convergence.confidence_tracker import ConfidenceTracker
from app.engines.convergence_controller import (
    GRAPH_INFERENCE_SOURCE,
    ConvergenceState,
    GraphConsumption,
    _apply_graph_targets,
)
from app.services import graph_return_channel as grc
from app.services.graph_return_channel import (
    CONFIDENCE_FLOOR,
    EnrichmentTarget,
    GraphReturnCapacityError,
    GraphReturnChannel,
    GraphReturnClaim,
    classify_packet,
    handle_graph_inference_result,
    intake_key,
    producer_key,
)

TENANT = "synthco"
T0 = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def _output(field: str = "capacity", value: Any = 2.5, confidence: Any = 0.9, **extra: Any):
    return {
        "entity_id": extra.pop("entity_id", "d-1"),
        "field": field,
        "value": value,
        "confidence": confidence,
        "rule": extra.pop("rule", "r1"),
        **extra,
    }


def _packet(outputs: list[Any], *, tenant: str = TENANT, key: str | None = "p-1"):
    return create_transport_packet(
        action="graph-inference-result",
        payload={"inference_outputs": outputs},
        tenant=tenant,
        source_node="gate",
        destination_node="enrichment-engine",
        reply_to="gate",
        idempotency_key=key,
    )


@pytest.fixture(autouse=True)
def _fresh_channel() -> Any:
    GraphReturnChannel.reset_instance()
    yield
    GraphReturnChannel.reset_instance()


# ── Intake binding and refusal (P24) ───────────────────────────────────────


def test_malformed_foreign_and_weak_outputs_are_refused_without_failing_the_packet() -> None:
    targets, rejections, recordable = classify_packet(
        _packet(
            [
                "not-a-map",
                {"field": "capacity", "value": 1, "confidence": 0.9},
                {"entity_id": "d-1", "value": 1, "confidence": 0.9},
                _output(entity_id=True),
                _output(confidence=True),
                _output(confidence="nan"),
                _output(confidence=1.5),
                _output(confidence="high"),
                _output(confidence=CONFIDENCE_FLOOR - 0.01),
                _output(tenant_id="othertenant"),
                _output(tenant="othertenant"),
                _output(field="kept", tenant_id=TENANT),
            ]
        )
    )
    assert [t.field_name for t in targets] == ["kept"]
    assert [r.reason for r in rejections] == [
        "malformed",
        "malformed",
        "malformed",
        "malformed",
        "malformed",
        "malformed",
        "malformed",
        "malformed",
        "below_threshold",
        "cross_tenant",
        "cross_tenant",
    ]
    # Only identifiable outputs are recorded; their rejection travels with them.
    assert [(r["field_name"], r["rejection"]) for r in recordable] == [
        ("capacity", "below_threshold"),
        ("capacity", "cross_tenant"),
        ("capacity", "cross_tenant"),
        ("kept", None),
    ]


def test_the_tenant_is_the_gate_packets_and_a_missing_rule_is_named() -> None:
    targets, _, _ = classify_packet(_packet([_output(rule=None)], tenant="acme"))
    assert targets[0].tenant_id == "acme"
    assert targets[0].origin_inference_rule == "unknown"


def test_the_producer_key_is_the_header_key_else_the_packet_id() -> None:
    assert producer_key(_packet([], key="idem-9")) == "idem-9"
    packet = _packet([], key=None)
    assert producer_key(packet) == f"packet:{packet.header.packet_id}"


def test_the_intake_key_binds_producer_key_and_the_whole_fact() -> None:
    (target,) = classify_packet(_packet([_output()]))[0]
    assert intake_key("p-1", target) == intake_key("p-1", target)
    assert intake_key("p-1", target) != intake_key("p-2", target)
    other_value = EnrichmentTarget(**{**target.to_dict(), "seed_value": 3.0})
    assert intake_key("p-1", other_value) != intake_key("p-1", target)


# ── In-process intake: idempotent and never half-accepted (P23, P28) ───────


@pytest.mark.asyncio
async def test_a_retransmit_is_queued_once_and_answered_the_same() -> None:
    packet = _packet([_output("a"), _output("b"), _output("a")])
    first = await handle_graph_inference_result(TENANT, packet.payload, packet)
    second = await handle_graph_inference_result(TENANT, packet.payload, packet)
    assert first["targets_queued"] == second["targets_queued"] == 2
    channel = GraphReturnChannel.get_instance()
    assert channel._queues[TENANT].qsize() == 2
    assert channel.stats()["duplicates"] == 4
    assert channel.stats()["durable"] is False


@pytest.mark.asyncio
async def test_a_full_queue_refuses_the_whole_packet_and_queues_nothing() -> None:
    channel = GraphReturnChannel(maxsize=3)
    assert await channel.submit(_packet([_output("a"), _output("b")], key="p-1")) == 2
    with pytest.raises(GraphReturnCapacityError):
        await channel.submit(_packet([_output("c"), _output("d")], key="p-2"))
    assert channel._queues[TENANT].qsize() == 2
    assert channel.stats()["refused_packets"] == 1
    # The refused packet is retried whole once there is room.
    channel.take_for_entity(TENANT, "d-1")
    assert await channel.submit(_packet([_output("c"), _output("d")], key="p-2")) == 2


def test_a_capacity_refusal_is_a_retryable_internal_error_not_a_bad_request() -> None:
    detail = classify_exception(GraphReturnCapacityError(TENANT, 10, 1, 10))
    assert (detail.status_code, detail.code) == (500, "internal_error")


@pytest.mark.asyncio
async def test_an_in_process_claim_takes_the_queue_and_settles_nothing() -> None:
    channel = GraphReturnChannel.get_instance()
    await channel.submit(_packet([_output()]))
    claim = await channel.claim_for_entity(TENANT, "d-1")
    assert not claim.durable
    assert [t.field_name for t in claim.targets] == ["capacity"]
    assert await channel.settle(claim, outcomes={}, rejections={}, consumed_by=None) == 0


# ── Durable intake through the store (P25, P27) ────────────────────────────


class _FakeStore:
    """Stands in for `pg_store`: records intake, hands out claims."""

    GraphReturnIntake = SimpleNamespace

    def __init__(self) -> None:
        self._engine = object()
        self.intakes: list[list[Any]] = []
        self.records: list[Any] = []
        self.settled: list[dict[str, Any]] = []
        self.refuse = False

    async def retain_graph_return_targets(self, intakes: list[Any], **kwargs: Any) -> Any:
        assert kwargs["capacity"] == grc.GRAPH_RETURN_TENANT_CAPACITY
        assert kwargs["ttl_seconds"] == grc.GRAPH_RETURN_TTL_SECONDS
        if self.refuse:
            raise GraphReturnCapacityError(TENANT, 1, len(intakes), 1)
        self.intakes.append(intakes)
        kept = sum(1 for i in intakes if i.rejection is None)
        return SimpleNamespace(
            retained=kept, duplicates=0, unchanged=0, rejected=len(intakes) - kept
        )

    async def claim_graph_return_targets(self, **kwargs: Any) -> list[Any]:
        assert kwargs["lease_seconds"] == grc.GRAPH_RETURN_LEASE_SECONDS
        return [r for r in self.records if r.entity_id == kwargs["entity_id"]]

    async def settle_graph_return_targets(self, **kwargs: Any) -> int:
        self.settled.append(kwargs)
        return len(kwargs["consumed"]) + len(kwargs["rejected"])


def _record(field: str, value: Any, confidence: float = 0.9, entity_id: str = "d-1") -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=TENANT,
        entity_id=entity_id,
        field_name=field,
        rule="r1",
        value=value,
        confidence=confidence,
        origin_packet_id="pkt-1",
        created_at=T0,
        lease_owner="converge#tok",
    )


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    fake = _FakeStore()
    monkeypatch.setattr(grc, "_durable_store", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_durable_intake_records_every_identifiable_output_with_its_binding(
    store: _FakeStore,
) -> None:
    packet = _packet([_output("kept"), _output("weak", confidence=0.1)], key="idem-7")
    result = await handle_graph_inference_result(TENANT, packet.payload, packet)
    assert result["targets_queued"] == 1
    assert set(result) == {"status", "targets_queued", "packet_id", "tenant"}
    (intakes,) = store.intakes
    assert [(i.field_name, i.rejection) for i in intakes] == [
        ("kept", None),
        ("weak", "below_threshold"),
    ]
    assert {i.producer_key for i in intakes} == {"idem-7"}
    assert {i.tenant_id for i in intakes} == {TENANT}
    assert GraphReturnChannel.get_instance()._queues == {}  # nothing in process memory


@pytest.mark.asyncio
async def test_a_durable_capacity_refusal_propagates(store: _FakeStore) -> None:
    store.refuse = True
    with pytest.raises(GraphReturnCapacityError):
        await GraphReturnChannel.get_instance().submit(_packet([_output()]))
    assert GraphReturnChannel.get_instance().stats()["refused_packets"] == 1


@pytest.mark.asyncio
async def test_a_durable_claim_carries_row_ids_and_settles_only_its_own(store: _FakeStore) -> None:
    store.records = [_record("capacity", 2.5), _record("other", 1, entity_id="d-2")]
    channel = GraphReturnChannel.get_instance()
    claim = await channel.claim_for_entity(TENANT, "d-1")
    assert claim.durable and claim.owner == "converge#tok"
    (target,) = claim.targets
    assert target.intake_id == str(store.records[0].id)
    foreign = str(uuid.uuid4())
    assert (
        await channel.settle(
            claim,
            outcomes={target.intake_id: "seeded", foreign: "seeded"},
            rejections={},
            consumed_by="converge:op-1",
        )
        == 1
    )
    (call,) = store.settled
    assert call["owner"] == "converge#tok"
    assert call["consumed"] == {uuid.UUID(target.intake_id): "seeded"}
    assert call["consumed_by"] == "converge:op-1"


@pytest.mark.asyncio
async def test_an_empty_durable_claim_settles_nothing(store: _FakeStore) -> None:
    claim = await GraphReturnChannel.get_instance().claim_for_entity(TENANT, "d-1")
    assert claim == GraphReturnClaim(owner=None, targets=[])
    assert (
        await GraphReturnChannel.get_instance().settle(
            claim, outcomes={}, rejections={}, consumed_by=None
        )
        == 0
    )


# ── Consumption: outcomes, provenance, no elevation (P27, P29) ─────────────


def _target(field: str, value: Any, confidence: float, intake_id: str | None = None):
    return EnrichmentTarget(
        entity_id="d-1",
        tenant_id=TENANT,
        field_name=field,
        seed_value=value,
        source_confidence=confidence,
        origin_packet_id="pkt-1",
        origin_inference_rule="r1",
        intake_id=intake_id or str(uuid.uuid4()),
    )


def _state(known: dict[str, Any], confidences: dict[str, float]) -> ConvergenceState:
    state = ConvergenceState()
    state.known_fields = dict(known)
    state.confidence_map = dict(confidences)
    return state


def test_each_leased_target_gets_an_outcome_and_a_seed_stays_a_graph_inference() -> None:
    state = _state({"name": "Acme", "region": "south"}, {"region": 0.95})
    tracker = ConfidenceTracker()
    ledger = GraphConsumption()
    entity_value = _target("name", "Other", 0.99)
    stronger_kept = _target("region", "north", 0.9)
    weaker_first = _target("capacity", 2.0, 0.7)
    stronger_second = _target("capacity", 2.5, 0.8)
    undeclared = _target("shoe_size", 9, 0.9)
    seeded = _apply_graph_targets(
        [entity_value, stronger_kept, weaker_first, stronger_second, undeclared],
        state,
        tracker,
        1,
        declared_fields=frozenset({"name", "region", "capacity"}),
        ledger=ledger,
    )
    assert seeded == {"capacity": 2.5}
    assert state.known_fields["name"] == "Acme"  # the record's own value wins
    assert state.known_fields["region"] == "south"
    assert ledger.outcomes == {
        entity_value.intake_id: "kept_entity_value",
        stronger_kept.intake_id: "kept_stronger_value",
        weaker_first.intake_id: "outranked",
        stronger_second.intake_id: "seeded",
    }
    assert ledger.rejections == {undeclared.intake_id: "field_not_in_schema"}
    # A graph value is an inference, never provider evidence or a verified field.
    assert tracker.fields["capacity"].source_variation == GRAPH_INFERENCE_SOURCE
    assert state.inferred_fields["capacity"] == 2.5


def test_without_a_declared_schema_no_target_is_refused_for_its_field() -> None:
    ledger = GraphConsumption()
    seeded = _apply_graph_targets(
        [_target("anything", 1, 0.9)], _state({}, {}), ConfidenceTracker(), 1, ledger=ledger
    )
    assert seeded == {"anything": 1}
    assert ledger.rejections == {}


def test_an_in_process_target_is_applied_but_recorded_nowhere() -> None:
    ledger = GraphConsumption()
    target = EnrichmentTarget(
        entity_id="d-1",
        tenant_id=TENANT,
        field_name="capacity",
        seed_value=1,
        source_confidence=0.9,
        origin_packet_id="pkt",
        origin_inference_rule="r",
    )
    assert _apply_graph_targets([target], _state({}, {}), ConfidenceTracker(), 1, ledger=ledger)
    assert (ledger.outcomes, ledger.rejections, ledger.seeded_by) == ({}, {}, {})


class _Channel:
    def __init__(self, claim: GraphReturnClaim | Exception, settle_error: bool = False) -> None:
        self.claim = claim
        self.settle_error = settle_error
        self.settled: list[dict[str, Any]] = []

    async def claim_for_entity(self, tenant_id: str, entity_id: str) -> GraphReturnClaim:
        if isinstance(self.claim, Exception):
            raise self.claim
        return self.claim

    async def settle(self, claim: GraphReturnClaim, **kwargs: Any) -> int:
        if self.settle_error:
            raise RuntimeError("store down")
        self.settled.append({"claim": claim, **kwargs})
        return 1


@pytest.mark.asyncio
async def test_a_store_failure_on_claim_leaves_the_converge_running_without_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(GraphReturnChannel, "get_instance", lambda: _Channel(OSError("down")))
    ledger = GraphConsumption()
    assert await cc._claim_graph_targets(ledger, TENANT, "d-1") == []
    assert ledger.claims == []


@pytest.mark.asyncio
async def test_consumption_is_settled_per_durable_claim_and_a_settle_failure_is_absorbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target("capacity", 2.5, 0.9)
    durable = GraphReturnClaim(owner="converge#tok", targets=[target])
    channel = _Channel(durable)
    monkeypatch.setattr(GraphReturnChannel, "get_instance", lambda: channel)
    ledger = GraphConsumption()
    assert await cc._claim_graph_targets(ledger, TENANT, "d-1") == [target]
    ledger.claims.append(GraphReturnClaim(owner=None, targets=[]))
    ledger.seed(target)
    await cc._settle_graph_consumption(ledger, "converge:op-1")
    (call,) = channel.settled
    assert call["claim"] is durable
    assert call["outcomes"] == {target.intake_id: "seeded"}
    assert call["consumed_by"] == "converge:op-1"
    channel.settle_error = True
    await cc._settle_graph_consumption(ledger, "converge:op-1")  # logged, not raised


def test_the_consumption_key_names_the_converge() -> None:
    assert cc._consumption_key(SimpleNamespace(idempotency_key="k-1"), TENANT, "d-1") == (
        "converge:k-1"
    )
    assert cc._consumption_key(SimpleNamespace(idempotency_key=None), TENANT, "d-1") == (
        f"converge:{TENANT}:d-1"
    )
    assert cc._consumption_key(SimpleNamespace(), None, None) is None


def test_declared_fields_come_only_from_a_spec_that_declares_some() -> None:
    assert cc._declared_fields(None) is None
    assert cc._declared_fields(SimpleNamespace(field_map={})) is None
    assert cc._declared_fields(SimpleNamespace(field_map={"a": 1, "b": 2})) == frozenset({"a", "b"})
