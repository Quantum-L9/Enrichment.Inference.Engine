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
    # A dotted id (`acme.recycling-eu`) is a legal tenant id under the identity
    # contract but DomainYamlReader._DOMAIN_ID_PATTERN still refuses dots; that
    # reader is owned by PR #235's lane, so the legal second tenant here is dashed.
    _write_spec(root, "acme-recycling-eu", _list_shaped_spec("acme-recycling-eu"))
    _write_spec(root, "Metals", _list_shaped_spec("Metals"))  # uppercase
    _write_spec(root, "systemwide", _list_shaped_spec("systemwide"))  # reserved prefix
    _write_spec(root, "ab", _list_shaped_spec("ab"))  # too short
    _write_spec(root, "under_score", _list_shaped_spec("under_score"))  # illegal char
    _write_spec(root, "renamed", _list_shaped_spec("something-else"))  # id mismatch
    (root / "broken").mkdir()
    (root / "broken" / "spec.yaml").write_text("ontology: [:::", encoding="utf-8")

    registry = converge_module.build_domain_spec_registry(DomainYamlReader(root))

    assert sorted(registry) == ["acme-recycling-eu", "plasticos"]
    assert registry["plasticos"]["ontology"]["nodes"][0]["label"] == "Facility"


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
    [("loaded", 200), ("not_attempted", 200), ("empty", 503), ("failed", 503)],
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
    saved: list[dict] = []
    reviewed: list[dict] = []

    async def fake_save(**kwargs):
        saved.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4(), **kwargs)

    async def fake_review(**kwargs):
        reviewed.append(kwargs)

    try:
        with (
            patch.object(converge_module.pg_store, "save_schema_proposal", side_effect=fake_save),
            patch.object(
                converge_module.pg_store, "approve_schema_proposal", side_effect=fake_review
            ),
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

    assert {s["field_name"] for s in saved} == {"annual_capacity_mt", "facility_tier"}
    assert all(s["tenant_id"] == s["domain"] == "plasticos" for s in saved)
    assert all(s["batch_run_id"] == state.run_id for s in saved)
    assert {(r["reviewed_by"], r["approved"]) for r in reviewed} == {
        ("ib@example.test", True),
        ("ib@example.test", False),
    }


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

        # An unreachable store leaves the file-backed revision in place.
        converge_module.configure(
            state_store=_RegistryStateStore(),
            profile_registry=ProfileRegistry(),
            domain_specs={"plasticos": _list_shaped_spec()},
        )
        with patch.object(
            converge_module.pg_store,
            "get_approved_schema_proposals",
            new=AsyncMock(side_effect=RuntimeError("Call init_engine() first")),
        ):
            assert await converge_module.hydrate_approved_revisions() == {}
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
