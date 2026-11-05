"""Owner evidence is captured at discovery and cannot be supplied at approval."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.v1 import converge, discover
from app.engines import handlers
from app.engines.convergence.loop_state import LoopState, LoopStatus
from app.engines.convergence.schema_proposer import ApprovalDecision


def _spec():
    return {
        "name": "acme",
        "version": "1.0.0",
        "ontology": {
            "nodes": [
                {"label": "Supplier", "properties": [{"name": "id", "type": "string"}]},
                {"label": "Buyer", "properties": [{"name": "id", "type": "string"}]},
            ]
        },
    }


@pytest.mark.parametrize("label", [None, "Missing", "Buyer OR 1=1"])
def test_discovery_requires_valid_explicit_owner_before_state(label, monkeypatch):
    monkeypatch.setattr(converge, "_domain_specs", {"acme": _spec()})
    with pytest.raises(HTTPException) as caught:
        converge._discovery_snapshot("acme", label)
    assert caught.value.status_code in {409, 422}


def test_owner_binding_survives_state_serialization():
    state = LoopState(
        domain="acme",
        node_label="Buyer",
        discovery_spec_sha256="a" * 64,
        ownership_contract_version=1,
    )
    loaded = LoopState.model_validate_json(state.model_dump_json())
    assert loaded.node_label == "Buyer"
    assert loaded.discovery_spec_sha256 == "a" * 64
    assert loaded.ownership_contract_version == 1


@pytest.mark.asyncio
async def test_unbound_historical_run_is_refused_before_audit(monkeypatch):
    state = LoopState(
        domain="acme", status=LoopStatus.HUMAN_HOLD, accumulated_fields={"shared_metric": 10}
    )
    audit = AsyncMock()
    monkeypatch.setattr(converge, "require_direct_domain_access", lambda: None)
    monkeypatch.setattr(converge, "_domain_specs", {"acme": _spec()})
    monkeypatch.setattr(
        converge, "_state_store", SimpleNamespace(load=AsyncMock(return_value=state))
    )
    monkeypatch.setattr(converge.pg_store, "persist_schema_proposal_batch", audit)
    with pytest.raises(HTTPException) as caught:
        await converge.approve_proposals(
            state.run_id,
            converge.ApproveRequestBody(
                decisions=[ApprovalDecision(field_name="shared_metric", node_label="Buyer")],
                reviewed_by="test-only-reviewer",
            ),
        )
    assert caught.value.status_code == 409
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_approve_by_id_maps_legacy_binding_refusal(monkeypatch):
    import uuid

    monkeypatch.setattr(
        discover.pg_store,
        "approve_schema_proposal",
        AsyncMock(side_effect=ValueError("Legacy unbound schema proposal")),
    )
    with pytest.raises(HTTPException) as caught:
        await discover.approve_proposal(
            uuid.uuid4(), discover.ApprovalRequest(approved=True, reviewed_by="test-only-reviewer")
        )
    assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_legacy_reference_duplicate_rule_refuses_whole_file(tmp_path, monkeypatch):
    reference = tmp_path / "rules.yaml"
    reference.write_text("inference_rules: []\ninference_rules:\n  - id: hidden\n")
    config = SimpleNamespace(
        inference_rules_path=str(reference),
        version="1.0.0",
        source_sha256="a" * 64,
        approval_ref=None,
        raw_spec={},
    )
    monkeypatch.setattr(
        handlers, "_domain_reader", SimpleNamespace(load=lambda _: config, require_published=False)
    )
    monkeypatch.setattr(handlers, "enrichment_hints_for", lambda *_: {})
    hints, rules = await handlers._load_domain_convergence_context(
        domain_id="acme", node_label="Buyer"
    )
    assert hints == {}
    assert isinstance(rules, str)
    assert "could not be loaded" in rules
    assert "Duplicate" in rules


def test_real_controller_evidence_retains_all_sources_without_a_winner():
    state = LoopState(domain="acme", accumulated_fields={"shared_metric": 10, "unobserved": 4})
    response = SimpleNamespace(
        feature_vector={
            "confidence_tracking": {
                "fields": {"shared_metric": {"confidence": 0.9}, "unobserved": {"confidence": 0.95}}
            }
        },
        inferences=[{"enriched": ["shared_metric"]}, {"inferred": ["shared_metric"]}],
    )
    converge._retain_discovery_evidence(state, response)
    assert state.observed_field_sources == {"shared_metric": ["enrichment", "inference"]}
    assert state.discovery_field_confidences == {"shared_metric": {"confidence": 0.9}}
    restored = LoopState.model_validate_json(state.model_dump_json())
    assert restored.observed_field_sources == state.observed_field_sources
    assert restored.discovery_field_confidences == state.discovery_field_confidences


@pytest.mark.asyncio
async def test_owner_bound_single_discovery_and_approval_preserve_provenance(monkeypatch):
    from app.core.config import Settings
    from app.models.schemas import EnrichResponse

    spec = _spec()
    stored = {}

    async def save(state):
        stored[state.run_id] = LoopState.model_validate_json(state.model_dump_json())

    async def load(run_id):
        return stored.get(run_id)

    state_store = SimpleNamespace(save=save, load=load)
    controller = AsyncMock(
        return_value=EnrichResponse(
            fields={"shared_metric": 10},
            confidence=0.9,
            pass_count=1,
            state="completed",
            feature_vector={
                "confidence_tracking": {"fields": {"shared_metric": {"confidence": 0.9}}}
            },
            inferences=[{"enriched": ["shared_metric"]}],
            tokens_used=10,
        )
    )
    audit = AsyncMock(side_effect=lambda **kw: [SimpleNamespace(**row) for row in kw["decisions"]])
    monkeypatch.setattr(converge, "require_direct_domain_access", lambda: None)
    monkeypatch.setattr(converge, "_domain_specs", {"acme": spec})
    monkeypatch.setattr(converge, "_state_store", state_store)
    monkeypatch.setattr("app.engines.convergence_controller.run_convergence_loop", controller)
    monkeypatch.setattr(converge.pg_store, "persist_schema_proposal_batch", audit)
    result = await converge.converge_single(
        converge.ConvergeRequestBody(
            entity={"id": "owner-test"},
            domain="acme",
            object_type="Account",
            node_label="Buyer",
            approval_mode="human",
        ),
        Settings(),
    )
    state = stored[result.run_id]
    assert state.node_label == "Buyer"  # object_type never selects ownership
    assert state.ownership_contract_version == 1
    assert state.status is LoopStatus.HUMAN_HOLD
    proposals = converge._propose_for_run(state, spec, "acme")
    field = proposals.proposed_fields[0]
    assert (field.node_label, field.field_name, field.source) == (
        "Buyer",
        "shared_metric",
        "enrichment",
    )
    assert field.discovery_spec_sha256 == state.discovery_spec_sha256
    approved = await converge.approve_proposals(
        result.run_id,
        converge.ApproveRequestBody(
            decisions=[ApprovalDecision(node_label="Buyer", field_name="shared_metric")],
            reviewed_by="test-only-reviewer",
        ),
    )
    assert approved["applied_fields"] == [{"node_label": "Buyer", "field_name": "shared_metric"}]
    row = audit.await_args.kwargs["decisions"][0]
    assert (row["node_label"], row["discovery_spec_sha256"], row["ownership_contract_version"]) == (
        "Buyer",
        state.discovery_spec_sha256,
        1,
    )
    updated = converge._domain_specs["acme"]
    assert [p["name"] for p in updated["ontology"]["nodes"][0]["properties"]] == ["id"]
    assert [p["name"] for p in updated["ontology"]["nodes"][1]["properties"]] == [
        "id",
        "shared_metric",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["wrong_owner", "missing_owner", "stale", "mixed_sources"])
async def test_bound_approval_negative_preflight_has_no_durable_side_effect(failure, monkeypatch):
    monkeypatch.setattr(converge, "require_direct_domain_access", lambda: None)
    monkeypatch.setattr(converge, "_domain_specs", {"acme": _spec()})
    _, digest = converge._discovery_snapshot("acme", "Buyer")
    state = LoopState(
        domain="acme",
        status=LoopStatus.HUMAN_HOLD,
        node_label="Buyer",
        discovery_spec_sha256=digest,
        ownership_contract_version=1,
        accumulated_fields={"shared_metric": 10},
        discovery_field_confidences={"shared_metric": {"confidence": 0.9}},
        observed_field_sources={"shared_metric": ["enrichment"]},
    )
    if failure == "stale":
        state.discovery_spec_sha256 = "b" * 64
    if failure == "mixed_sources":
        state.observed_field_sources["shared_metric"] = ["enrichment", "inference"]
    owner = (
        "Supplier" if failure == "wrong_owner" else None if failure == "missing_owner" else "Buyer"
    )
    state_save = AsyncMock()
    audit = AsyncMock()
    monkeypatch.setattr(
        converge,
        "_state_store",
        SimpleNamespace(load=AsyncMock(return_value=state), save=state_save),
    )
    monkeypatch.setattr(converge.pg_store, "persist_schema_proposal_batch", audit)
    with pytest.raises(HTTPException) as caught:
        await converge.approve_proposals(
            state.run_id,
            converge.ApproveRequestBody(
                decisions=[ApprovalDecision(node_label=owner, field_name="shared_metric")],
                reviewed_by="test-only-reviewer",
            ),
        )
    assert caught.value.status_code in {409, 422}
    assert state.status is LoopStatus.HUMAN_HOLD
    audit.assert_not_awaited()
    state_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_redis_active_listing_includes_owner_bound_human_hold():
    from app.engines.convergence.loop_state import RedisLoopStateStore

    held = LoopState(
        domain="acme",
        status=LoopStatus.HUMAN_HOLD,
        node_label="Buyer",
        discovery_spec_sha256="a" * 64,
        ownership_contract_version=1,
    )
    complete = LoopState(domain="acme", status=LoopStatus.CONVERGED)
    raw = {f"enrich:loop:{s.run_id}": s.model_dump_json() for s in (held, complete)}

    class RedisFixture:
        async def scan_iter(self, **_kwargs):
            for key in raw:
                yield key

        async def get(self, key):
            return raw.get(key)

    states = await RedisLoopStateStore(RedisFixture()).list_active("acme")
    assert [state.run_id for state in states] == [held.run_id]
    assert states[0].node_label == "Buyer"
    assert states[0].discovery_spec_sha256 == "a" * 64


@pytest.mark.asyncio
async def test_postgres_active_listing_queries_both_running_and_human_hold():
    from app.engines.convergence.loop_state import PostgresLoopStateStore

    queries = []

    class Connection:
        async def fetch(self, query, *args):
            queries.append((query, args))
            return []

    class Acquisition:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return False

    pool = SimpleNamespace(acquire=Acquisition)
    assert await PostgresLoopStateStore(pool).list_active("acme") == []
    assert "human_hold" in queries[0][0]
    assert "running" in queries[0][0]
    assert queries[0][1] == ("acme",)


@pytest.mark.asyncio
async def test_owner_bound_batch_can_hold_real_discovery_for_review(monkeypatch):
    from app.core.config import Settings
    from app.models.schemas import EnrichResponse
    from app.services.enrichment_profile import EnrichmentProfile

    stored = {}

    async def save(state):
        stored[state.run_id] = LoopState.model_validate_json(state.model_dump_json())

    controller = AsyncMock(
        return_value=EnrichResponse(
            fields={"shared_metric": 10},
            pass_count=1,
            state="completed",
            feature_vector={
                "confidence_tracking": {"fields": {"shared_metric": {"confidence": 0.9}}}
            },
            inferences=[{"enriched": ["shared_metric"]}],
        )
    )
    monkeypatch.setattr(converge, "require_direct_domain_access", lambda: None)
    monkeypatch.setattr(converge, "_domain_specs", {"acme": _spec()})
    monkeypatch.setattr(converge, "_state_store", SimpleNamespace(save=save))
    monkeypatch.setattr(converge, "_profile_registry", SimpleNamespace())
    monkeypatch.setattr("app.engines.convergence_controller.run_convergence_loop", controller)
    result = await converge.converge_batch(
        converge.BatchConvergeRequestBody(
            inline_profile=EnrichmentProfile(profile_name="test-only-owner"),
            domain="acme",
            node_label="Buyer",
            approval_mode="human",
            entities=[{"id": "batch-owner", "object_type": "Account"}],
        ),
        Settings(),
    )
    state = stored[result.run_ids[0]]
    assert state.status is LoopStatus.HUMAN_HOLD
    assert state.node_label == "Buyer"
    assert state.ownership_contract_version == 1
    assert state.discovery_spec_sha256
    assert state.observed_field_sources == {"shared_metric": ["enrichment"]}
