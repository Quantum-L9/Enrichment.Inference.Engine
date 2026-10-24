"""
Integration tests for the /v1/converge endpoint.
Uses mocked LLM calls to avoid external API costs in CI.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import get_settings

_TEST_API_KEY = "pass"
# Precomputed sha256(_TEST_API_KEY) — the same literal digest already used by
# tests/test_api.py, tests/test_rate_limiter.py and tests/services/
# test_gate_registration.py. Stored rather than computed so no hashing call
# exists here at all: hashing a fixture key inline is what CodeQL flags as
# py/weak-sensitive-data-hashing (SHA-256 is not a computationally expensive
# password hash), and usedforsecurity=False does not suppress that query.
# app/core/auth.py still does the real comparison via hmac.compare_digest.
os.environ["API_KEY_HASH"] = "d74ff0ee8da3b9806b18c877dbf29bbde50b5bd8e4dad7a3a725000feb82e8f1"
get_settings.cache_clear()
AUTH = {"X-API-Key": _TEST_API_KEY}

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_converge_health(api_client):
    """Health endpoint returns 200."""
    resp = await api_client.get("/api/v1/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
# enrichment_orchestrator does `from ..services.perplexity_client import
# query_perplexity`, so the alias is already bound by the time this test runs
# (the api_client fixture imports app.main). Patching the *source* module would
# leave that binding untouched and let the call reach the live Sonar API —
# patch the name the orchestrator actually calls.
@patch("app.engines.enrichment_orchestrator.query_perplexity", new_callable=AsyncMock)
async def test_converge_single_entity(mock_llm, api_client):
    """POST /v1/converge converges and returns the ConvergeSingleResponse shape."""
    import app.api.v1.converge as converge_module
    from app.engines.convergence.loop_state import LoopState, LoopStateStore
    from app.services.enrichment_profile import ProfileRegistry
    from app.services.kb_resolver import KBResolver
    from app.services.perplexity_client import SonarResponse

    # The pass schema is built from the planner's priority fields, which with
    # no domain spec are the entity's own keys; a provider answer that omits
    # them is dropped by schema validation. Before the consensus engine
    # stopped treating the `confidence` metadata key as an enriched field,
    # this test passed on that leaked key alone.
    mock_llm.return_value = SonarResponse(
        data={
            "id": "test-001",
            "company_name": "Alpha Recyclers Inc.",
            "materials_handled": ["HDPE", "PP"],
            "material_grade": "A",
            "contamination_tolerance_pct": 0.02,
            "facility_tier": "tier_1",
            "confidence": 0.9,
        },
        tokens_used=1200,
        citations=["https://example.com/alpha-recyclers"],
        model="sonar",
    )

    class _MemoryStateStore(LoopStateStore):
        """Minimal in-process store so the endpoint is configured (GAP-3)."""

        def __init__(self) -> None:
            self._states: dict[str, LoopState] = {}

        async def save(self, state: LoopState) -> None:
            self._states[state.run_id] = state

        async def load(self, run_id: str) -> LoopState | None:
            return self._states.get(run_id)

        async def list_active(self, domain: str | None = None) -> list[LoopState]:
            return list(self._states.values())

    payload = {
        "entity": {
            "id": "test-001",
            "company_name": "Alpha Recyclers",
            "materials_handled": ["HDPE"],
        },
        "object_type": "Account",
        "domain": "plasticos",
        "objective": "Full entity enrichment",
        "max_passes": 2,
        "max_budget_tokens": 5000,
    }

    # configure() writes five module-level globals; snapshot and restore all of
    # them so this test cannot leak wiring into whatever runs next.
    _configured = (
        "_state_store",
        "_profile_registry",
        "_domain_specs",
        "_kb_resolver",
        "_idem_store",
    )
    original = {name: getattr(converge_module, name) for name in _configured}
    converge_module.configure(
        state_store=_MemoryStateStore(),
        profile_registry=ProfileRegistry(),
        domain_specs={},
        # Mirrors app.main's wiring. KBResolver tolerates a missing kb_dir
        # (logs a warning, resolves to empty), so this stays hermetic.
        kb_resolver=KBResolver(get_settings().kb_dir),
        idem_store=None,
    )
    try:
        resp = await api_client.post("/v1/converge", json=payload, headers=AUTH)
    finally:
        for name, value in original.items():
            setattr(converge_module, name, value)

    # Configured store + mocked LLM: a 503 or 422 here is a real regression,
    # not an accepted outcome.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "run_id",
        "status",
        "passes_completed",
        "fields_discovered",
        "tokens_used",
        "cost_usd",
        "convergence_reason",
    }
    assert body["run_id"]
    assert body["status"] == "converged"
    assert body["passes_completed"] >= 1
    assert body["fields_discovered"] >= 1
    assert body["tokens_used"] > 0
    assert mock_llm.await_count >= 1


@pytest.mark.asyncio
async def test_scan_endpoint_rejects_unknown_domain_without_auth(api_client):
    """Auth runs first — unauthenticated POST /v1/scan is 401."""
    resp = await api_client.post(
        "/v1/scan",
        json={
            "fields": [{"name": "company_name", "field_type": "string"}],
            "domain": "nonexistent-domain-xyz",
        },
        headers={"X-API-Key": ""},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_scan_endpoint_rejects_unknown_domain(api_client):
    """Authenticated POST /v1/scan answers 404 for an unregistered domain.

    EIE-IDENT-02: both scan endpoints resolve the domain through
    converge.get_domain_spec, and 404 is the status the OpenAPI contract
    documents for an unknown domain.
    """
    resp = await api_client.post(
        "/v1/scan",
        json={
            "fields": [{"name": "company_name", "field_type": "string"}],
            "domain": "nonexistent-domain-xyz",
        },
        headers=AUTH,
    )
    assert resp.status_code == 404
    assert "nonexistent-domain-xyz" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_scan_endpoints_agree_on_unknown_domain(api_client):
    """POST /v1/scan and POST /api/v1/scan give the same status and detail."""
    v1 = await api_client.post(
        "/v1/scan",
        json={"fields": [], "domain": "nonexistent-domain-xyz"},
        headers=AUTH,
    )
    api_v1 = await api_client.post(
        "/api/v1/scan",
        json={"fields": [], "domain": "nonexistent-domain-xyz", "tenant_id": "tenant-1"},
        headers=AUTH,
    )
    assert (v1.status_code, api_v1.status_code) == (404, 404)
    assert v1.json()["detail"] == api_v1.json()["detail"]


# ── EIE-IDENT-02: the domain registry is domains/, one spec per tenant ───────


def _write_spec(root, domain_dir: str, spec: dict) -> None:
    import yaml

    (root / domain_dir).mkdir(parents=True)
    (root / domain_dir / "spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")


def _list_shaped_spec(name: str = "plasticos") -> dict:
    """The shape domains/*/spec.yaml carry: nodes and properties are lists."""
    return {
        "name": name,
        "version": "0.3.0",
        "ontology": {
            "nodes": [
                {
                    "label": "Facility",
                    "properties": [{"name": "id", "type": "string"}],
                    "enrichment_hints": {"priority_fields": ["id"]},
                }
            ]
        },
    }


def test_domain_spec_registry_keys_specs_by_legal_tenant_id(tmp_path):
    """Directory name == tenant id == spec id == Neo4j database name, or refused."""
    import app.api.v1.converge as converge_module
    from app.engines.domain_yaml_reader import DomainYamlReader

    root = tmp_path / "domains"
    _write_spec(root, "plasticos", _list_shaped_spec("plasticos"))
    # Dotted ids are legal Neo4j database names; the reader and the registry
    # share one grammar (domain_yaml_reader.is_legal_tenant_id), so `acme.eu`
    # loads and registers.
    _write_spec(root, "acme.eu", _list_shaped_spec("acme.eu"))
    _write_spec(root, "Metals", _list_shaped_spec("Metals"))  # uppercase
    _write_spec(root, "systemwide", _list_shaped_spec("systemwide"))  # reserved prefix
    _write_spec(root, "ab", _list_shaped_spec("ab"))  # too short
    _write_spec(root, "under_score", _list_shaped_spec("under_score"))  # illegal char
    _write_spec(root, "renamed", _list_shaped_spec("something-else"))  # id mismatch
    (root / "broken").mkdir()
    (root / "broken" / "spec.yaml").write_text("ontology: [:::", encoding="utf-8")
    # Valid YAML, wrong shape: a list root and `ontology.nodes` as a mapping
    # raise AttributeError inside DomainYamlReader._parse. Each refuses its own
    # tenant only.
    (root / "listroot").mkdir()
    (root / "listroot" / "spec.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    _write_spec(root, "mapnodes", {"name": "mapnodes", "ontology": {"nodes": {"Facility": {}}}})

    registry = converge_module.build_domain_spec_registry(DomainYamlReader(root))

    assert sorted(registry) == ["acme.eu", "plasticos"]
    assert registry["plasticos"]["ontology"]["nodes"][0]["label"] == "Facility"
    assert DomainYamlReader(root).load("acme.eu").domain_id == "acme.eu"


def test_one_structurally_broken_tenant_does_not_fail_the_registry(tmp_path, monkeypatch):
    """main._load_domain_registry: the valid tenant registers and the state is `loaded`."""
    from app import main as main_module
    from app.engines.domain_yaml_reader import DomainYamlReader

    root = tmp_path / "domains"
    _write_spec(root, "plasticos", _list_shaped_spec("plasticos"))
    (root / "listroot").mkdir()
    (root / "listroot" / "spec.yaml").write_text("- just\n- a list\n", encoding="utf-8")

    monkeypatch.setattr(main_module, "_domain_registry_state", "not_attempted")
    registry = main_module._load_domain_registry(DomainYamlReader(root))

    assert sorted(registry) == ["plasticos"]
    assert main_module._domain_registry_state == "loaded"


def test_repo_domains_root_registers_plasticos():
    """The checked-in domains/ yields the live tenant under its own id."""
    from pathlib import Path

    import app.api.v1.converge as converge_module
    from app.engines.domain_yaml_reader import DomainYamlReader

    root = Path(__file__).resolve().parents[2] / "domains"
    registry = converge_module.build_domain_spec_registry(DomainYamlReader(root))
    assert "plasticos" in registry
    assert registry["plasticos"]["inference_rules"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("registry_state", "expected_http"),
    [
        ("loaded", 200),
        ("not_attempted", 200),
        ("empty", 503),
        ("failed", 503),
        ("hydrate_failed", 503),
    ],
)
async def test_readiness_reflects_domain_registry(
    api_client, monkeypatch, registry_state, expected_http
):
    """A routable node with no domain spec can serve no tenant: not Ready."""
    from app import main as main_module

    monkeypatch.setenv("GATE_REGISTRATION_ENABLED", "false")
    monkeypatch.setattr(main_module, "_domain_registry_state", registry_state)
    get_settings.cache_clear()
    try:
        resp = await api_client.get(main_module.READINESS_ENDPOINT)
    finally:
        get_settings.cache_clear()
    assert resp.status_code == expected_http
    assert resp.json()["ready"] is (expected_http == 200)


# ── EIE-IDENT-03: approved revisions persist via schema_proposals ────────────


class _RegistryStateStore:
    """In-process LoopStateStore for the approval tests."""

    def __init__(self, *states) -> None:
        self._states = {s.run_id: s for s in states}

    async def save(self, state) -> None:
        self._states[state.run_id] = state

    async def load(self, run_id: str):
        return self._states.get(run_id)

    async def list_active(self, domain=None):
        return list(self._states.values())


def _held_state(domain: str = "plasticos"):
    from app.engines.convergence.loop_state import LoopState, LoopStatus
    from app.models.field_confidence import FieldConfidence, FieldConfidenceMap, FieldSource

    fcm = FieldConfidenceMap()
    fcm.set(
        FieldConfidence(
            field_name="annual_capacity_mt",
            value=5000.0,
            confidence=0.9,
            source=FieldSource.ENRICHMENT,
            pass_discovered=1,
        )
    )
    fcm.set(
        FieldConfidence(
            field_name="facility_tier",
            value="tier_1",
            confidence=0.88,
            source=FieldSource.INFERENCE,
            pass_discovered=1,
        )
    )
    return LoopState(
        entity_id="ent-001",
        domain=domain,
        status=LoopStatus.HUMAN_HOLD,
        accumulated_fields={"annual_capacity_mt": 5000.0, "facility_tier": "tier_1"},
        accumulated_confidences=fcm,
    )


def _configure_for_approval(converge_module, state, spec):
    from app.services.enrichment_profile import ProfileRegistry

    names = ("_state_store", "_profile_registry", "_domain_specs", "_kb_resolver", "_idem_store")
    original = {name: getattr(converge_module, name) for name in names}
    converge_module.configure(
        state_store=_RegistryStateStore(state),
        profile_registry=ProfileRegistry(),
        domain_specs={state.domain: spec},
    )
    return original


def _property_names(spec: dict) -> set[str]:
    props = spec["ontology"]["nodes"][0]["properties"]
    return {p["name"] for p in props}


@pytest.mark.asyncio
async def test_approve_persists_the_revision_before_the_registry_takes_it(api_client):
    """Approved fields are written to schema_proposals (tenant == domain) and applied."""
    import uuid
    from types import SimpleNamespace

    import app.api.v1.converge as converge_module

    state = _held_state()
    original = _configure_for_approval(converge_module, state, _list_shaped_spec())
    batches: list[dict] = []

    async def fake_batch(**kwargs):
        # One call, one transaction: every decided row of the approval arrives together.
        batches.append(kwargs)
        return [SimpleNamespace(id=uuid.uuid4(), **row) for row in kwargs["decisions"]]

    try:
        with patch.object(
            converge_module.pg_store, "persist_schema_proposal_batch", side_effect=fake_batch
        ):
            resp = await api_client.post(
                f"/v1/converge/{state.run_id}/approve",
                json={
                    "decisions": [
                        {"field_name": "annual_capacity_mt", "approved": True},
                        {"field_name": "facility_tier", "approved": False},
                    ],
                    "reviewed_by": "ib@example.test",
                },
                headers=AUTH,
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert (body["approved"], body["rejected"], body["persisted"]) == (1, 1, 2)
        assert body["new_version"] == "0.4.0-discovered"

        revised = converge_module._domain_specs["plasticos"]
        # List-shaped ontology: the approved field landed, the rejected one did not.
        assert _property_names(revised) == {"id", "annual_capacity_mt"}
        assert revised["version"] == "0.4.0-discovered"
    finally:
        for name, value in original.items():
            setattr(converge_module, name, value)

    assert len(batches) == 1, "the whole approval is one store transaction"
    sent = batches[0]
    assert sent["tenant_id"] == sent["domain"] == "plasticos"
    assert sent["batch_run_id"] == state.run_id
    assert sent["reviewed_by"] == "ib@example.test"
    assert {(d["field_name"], d["approved"]) for d in sent["decisions"]} == {
        ("annual_capacity_mt", True),
        ("facility_tier", False),
    }


@pytest.mark.asyncio
async def test_concurrent_approvals_for_one_domain_keep_both_field_sets(api_client):
    """The per-domain lock serialises approvals: the second lands on the first's revision."""
    import asyncio
    import uuid
    from types import SimpleNamespace

    import app.api.v1.converge as converge_module
    from app.engines.convergence.loop_state import LoopState, LoopStatus
    from app.models.field_confidence import FieldConfidence, FieldConfidenceMap, FieldSource
    from app.services.enrichment_profile import ProfileRegistry

    def _held(field: str, value) -> LoopState:
        fcm = FieldConfidenceMap()
        fcm.set(
            FieldConfidence(
                field_name=field, value=value, confidence=0.9, source=FieldSource.ENRICHMENT
            )
        )
        return LoopState(
            entity_id=f"ent-{field}",
            domain="plasticos",
            status=LoopStatus.HUMAN_HOLD,
            accumulated_fields={field: value},
            accumulated_confidences=fcm,
        )

    first, second = _held("annual_capacity_mt", 5000.0), _held("certifications", "ISO")
    names = ("_state_store", "_profile_registry", "_domain_specs", "_kb_resolver", "_idem_store")
    original = {name: getattr(converge_module, name) for name in names}
    converge_module.configure(
        state_store=_RegistryStateStore(first, second),
        profile_registry=ProfileRegistry(),
        domain_specs={"plasticos": _list_shaped_spec()},
    )

    async def slow_batch(**kwargs):
        # Long enough for both requests to be in flight: without the lock, both
        # compute their revision from the same snapshot and the last writer wins.
        await asyncio.sleep(0.05)
        return [SimpleNamespace(id=uuid.uuid4(), **row) for row in kwargs["decisions"]]

    try:
        with patch.object(
            converge_module.pg_store, "persist_schema_proposal_batch", side_effect=slow_batch
        ):
            responses = await asyncio.gather(
                api_client.post(
                    f"/v1/converge/{first.run_id}/approve",
                    json={"decisions": [{"field_name": "annual_capacity_mt", "approved": True}]},
                    headers=AUTH,
                ),
                api_client.post(
                    f"/v1/converge/{second.run_id}/approve",
                    json={"decisions": [{"field_name": "certifications", "approved": True}]},
                    headers=AUTH,
                ),
            )
        assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
        revised = converge_module._domain_specs["plasticos"]
        assert _property_names(revised) == {"id", "annual_capacity_mt", "certifications"}
        assert revised["version"] == "0.5.0-discovered", "two approvals, two version bumps"
    finally:
        for name, value in original.items():
            setattr(converge_module, name, value)


@pytest.mark.asyncio
async def test_approve_fails_closed_when_the_store_is_unavailable(api_client):
    """No persisted revision, no applied revision: 503 and the registry is untouched."""
    import app.api.v1.converge as converge_module

    state = _held_state()
    spec = _list_shaped_spec()
    original = _configure_for_approval(converge_module, state, spec)
    try:
        # pg_store has no engine in this process: get_session raises RuntimeError.
        resp = await api_client.post(
            f"/v1/converge/{state.run_id}/approve",
            json={"decisions": [{"field_name": "annual_capacity_mt", "approved": True}]},
            headers=AUTH,
        )
        assert resp.status_code == 503, resp.text
        assert converge_module._domain_specs["plasticos"] is spec
        assert _property_names(spec) == {"id"}
        assert spec["version"] == "0.3.0"
        assert (await converge_module._state_store.load(state.run_id)).status.value == "human_hold"
    finally:
        for name, value in original.items():
            setattr(converge_module, name, value)


def _approved_row(field_name: str, field_type: str, batch: str, **extra):
    from types import SimpleNamespace

    return SimpleNamespace(
        tenant_id="plasticos",
        domain="plasticos",
        batch_run_id=batch,
        field_name=field_name,
        field_type=field_type,
        source="enrichment",
        fill_rate=1.0,
        avg_confidence=0.9,
        sample_values=[],
        value_distribution={},
        proposed_gate=extra.get("proposed_gate"),
        proposed_scoring_dimension=extra.get("proposed_scoring_dimension"),
        approval_status="approved",
    )


@pytest.mark.asyncio
async def test_hydrate_replays_approved_rows_onto_the_registry():
    """Startup reads the approved revision back; each batch is one version bump."""
    import app.api.v1.converge as converge_module
    from app.services.enrichment_profile import ProfileRegistry

    names = ("_state_store", "_profile_registry", "_domain_specs", "_kb_resolver", "_idem_store")
    original = {name: getattr(converge_module, name) for name in names}
    rows = [
        _approved_row("annual_capacity_mt", "float", "batch-1"),
        _approved_row("facility_tier", "string", "batch-2", proposed_gate="categorical"),
    ]
    converge_module.configure(
        state_store=_RegistryStateStore(),
        profile_registry=ProfileRegistry(),
        domain_specs={"plasticos": _list_shaped_spec()},
    )
    try:
        with patch.object(
            converge_module.pg_store,
            "get_approved_schema_proposals",
            new=AsyncMock(return_value=rows),
        ) as mock_get:
            applied = await converge_module.hydrate_approved_revisions()
        assert applied == {"plasticos": 2}
        mock_get.assert_awaited_once_with(tenant_id="plasticos", domain="plasticos")
        revised = converge_module._domain_specs["plasticos"]
        assert _property_names(revised) == {"id", "annual_capacity_mt", "facility_tier"}
        assert revised["version"] == "0.5.0-discovered"  # two batches, two bumps
        assert {"field": "facility_tier", "type": "categorical", "auto_proposed": True} in revised[
            "gates"
        ]

        # An unreachable store leaves the file-backed revision in place and is
        # raised, not swallowed: main records `hydrate_failed` (not Ready).
        converge_module.configure(
            state_store=_RegistryStateStore(),
            profile_registry=ProfileRegistry(),
            domain_specs={"plasticos": _list_shaped_spec()},
        )
        with (
            patch.object(
                converge_module.pg_store,
                "get_approved_schema_proposals",
                new=AsyncMock(side_effect=RuntimeError("Call init_engine() first")),
            ),
            pytest.raises(converge_module.SchemaRevisionHydrateError, match="plasticos"),
        ):
            await converge_module.hydrate_approved_revisions()
        assert converge_module._domain_specs["plasticos"]["version"] == "0.3.0"
    finally:
        for name, value in original.items():
            setattr(converge_module, name, value)


def test_apply_serves_list_shaped_and_dict_shaped_ontologies():
    """schema_proposer.apply writes into whichever shape the spec declares."""
    from app.engines.convergence.schema_proposer import (
        ApprovalDecision,
        FieldProposal,
        SchemaProposalSet,
        apply,
    )

    proposal = SchemaProposalSet(
        proposed_fields=[FieldProposal(field_name="annual_capacity_mt", field_type="float")],
        version_bump="0.4.0-discovered",
    )
    decisions = [ApprovalDecision(field_name="annual_capacity_mt", approved=True)]

    listed = apply(_list_shaped_spec(), decisions, proposal)
    assert {"name": "annual_capacity_mt", "type": "float", "source": "enrichment"} in listed[
        "ontology"
    ]["nodes"][0]["properties"]

    dict_spec = {"version": "0.3.0", "ontology": {"nodes": {"Facility": {"properties": {}}}}}
    dicted = apply(dict_spec, decisions, proposal)
    assert dicted["ontology"]["nodes"]["Facility"]["properties"]["annual_capacity_mt"] == {
        "type": "float",
        "source": "enrichment",
    }


# ── F-247-2: a batch that applies nothing publishes no revision ──────────────


def _held_for(field: str, value):
    from app.engines.convergence.loop_state import LoopState, LoopStatus
    from app.models.field_confidence import FieldConfidence, FieldConfidenceMap, FieldSource

    fcm = FieldConfidenceMap()
    fcm.set(
        FieldConfidence(
            field_name=field, value=value, confidence=0.9, source=FieldSource.ENRICHMENT
        )
    )
    return LoopState(
        entity_id=f"ent-{field}",
        domain="plasticos",
        status=LoopStatus.HUMAN_HOLD,
        accumulated_fields={field: value},
        accumulated_confidences=fcm,
    )


class _ProposalTable:
    """One in-memory `schema_proposals` table behind both store operations."""

    def __init__(self) -> None:
        self.rows: list = []

    async def persist(self, **kwargs):
        from types import SimpleNamespace

        written = []
        for row in kwargs["decisions"]:
            fields = {k: v for k, v in row.items() if k != "approved"}
            record = SimpleNamespace(
                tenant_id=kwargs["tenant_id"],
                domain=kwargs["domain"],
                batch_run_id=kwargs["batch_run_id"],
                approval_status="approved" if row["approved"] else "rejected",
                **fields,
            )
            self.rows.append(record)
            written.append(record)
        return written

    async def approved(self, *, tenant_id: str, domain: str):
        return [
            r
            for r in self.rows
            if r.approval_status == "approved" and (r.tenant_id, r.domain) == (tenant_id, domain)
        ]


@pytest.mark.asyncio
async def test_unapplied_approvals_publish_no_revision_so_replay_matches_live(api_client):
    """All-rejected and unproposed-field approvals bump nothing; a restart lands on live."""
    import copy

    import app.api.v1.converge as converge_module
    from app.services.enrichment_profile import ProfileRegistry

    rejected = _held_for("annual_capacity_mt", 5000.0)
    unproposed = _held_for("certifications", "ISO")
    applied = _held_for("facility_tier", "tier_1")
    table = _ProposalTable()
    names = ("_state_store", "_profile_registry", "_domain_specs", "_kb_resolver", "_idem_store")
    original = {name: getattr(converge_module, name) for name in names}
    converge_module.configure(
        state_store=_RegistryStateStore(rejected, unproposed, applied),
        profile_registry=ProfileRegistry(),
        domain_specs={"plasticos": _list_shaped_spec()},
    )
    sequence = [
        (rejected, [{"field_name": "annual_capacity_mt", "approved": False}]),
        (unproposed, [{"field_name": "not_a_proposed_field", "approved": True}]),
        (applied, [{"field_name": "facility_tier", "approved": True}]),
    ]
    try:
        with (
            patch.object(
                converge_module.pg_store, "persist_schema_proposal_batch", side_effect=table.persist
            ),
            patch.object(
                converge_module.pg_store,
                "get_approved_schema_proposals",
                side_effect=table.approved,
            ),
        ):
            responses = [
                await api_client.post(
                    f"/v1/converge/{state.run_id}/approve",
                    json={"decisions": decisions},
                    headers=AUTH,
                )
                for state, decisions in sequence
            ]
            live = copy.deepcopy(converge_module._domain_specs["plasticos"])
            # Restart: the file-backed revision, then the approved rows replayed.
            converge_module.configure(
                state_store=_RegistryStateStore(),
                profile_registry=ProfileRegistry(),
                domain_specs={"plasticos": _list_shaped_spec()},
            )
            await converge_module.hydrate_approved_revisions()
            replayed = converge_module._domain_specs["plasticos"]
    finally:
        for name, value in original.items():
            setattr(converge_module, name, value)

    assert [r.status_code for r in responses] == [200, 200, 200], [r.text for r in responses]
    bodies = [r.json() for r in responses]
    assert [b["approved"] for b in bodies] == [0, 0, 1], "applied counts, not decided counts"
    assert [b["new_version"] for b in bodies] == ["0.3.0", "0.3.0", "0.4.0-discovered"]
    assert bodies[1]["unapplied"] == ["not_a_proposed_field"]
    assert replayed["version"] == live["version"]
    assert replayed == live


# ── F-247-3: a registry scan carries the tenant's identity ───────────────────


@pytest.mark.asyncio
async def test_scans_of_two_tenants_carry_distinct_identities(api_client, monkeypatch):
    """Same ontology, two tenants: domain_id, version and hashes come from the registry key."""
    import app.api.v1.converge as converge_module

    monkeypatch.setattr(
        converge_module,
        "_domain_specs",
        {"plasticos": _list_shaped_spec("plasticos"), "acme": _list_shaped_spec("acme")},
    )
    scans = {}
    reports = {}
    for tenant in ("plasticos", "acme"):
        scans[tenant] = await api_client.post(
            "/api/v1/scan",
            json={
                "fields": [{"name": "id", "type": "string"}],
                "domain": tenant,
                "tenant_id": tenant,
            },
            headers=AUTH,
        )
        reports[tenant] = await api_client.post(
            "/v1/scan",
            json={"fields": [{"name": "id", "field_type": "string"}], "domain": tenant},
            headers=AUTH,
        )

    plasticos, acme = scans["plasticos"].json(), scans["acme"].json()
    assert plasticos["domain_id"] == "plasticos"
    assert plasticos["domain_version"] == "0.3.0"
    assert acme["domain_id"] == "acme"
    assert plasticos["scan_hash"] != acme["scan_hash"]
    assert reports["plasticos"].json()["domain_id"] == "plasticos"
    assert reports["acme"].json()["domain_id"] == "acme"
    assert reports["plasticos"].json()["report_hash"] != reports["acme"].json()["report_hash"]


# ── F-247-10: a replay that cannot run is not Ready ──────────────────────────


@pytest.mark.asyncio
async def test_startup_hydrate_failure_degrades_readiness(api_client, monkeypatch):
    """get_approved_schema_proposals raising at startup -> `hydrate_failed` -> 503."""
    import app.api.v1.converge as converge_module
    from app import main as main_module

    monkeypatch.setattr(converge_module, "_domain_specs", {"plasticos": _list_shaped_spec()})
    monkeypatch.setattr(main_module, "_domain_registry_state", "loaded")
    monkeypatch.setenv("GATE_REGISTRATION_ENABLED", "false")
    with patch.object(
        converge_module.pg_store,
        "get_approved_schema_proposals",
        new=AsyncMock(side_effect=RuntimeError("store down")),
    ):
        applied = await main_module._hydrate_domain_registry()
    get_settings.cache_clear()
    try:
        resp = await api_client.get(main_module.READINESS_ENDPOINT)
    finally:
        get_settings.cache_clear()

    assert applied == {}
    assert main_module._domain_registry_state == "hydrate_failed"
    assert resp.status_code == 503
