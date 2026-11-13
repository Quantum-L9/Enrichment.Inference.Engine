"""
Integration tests for the /v1/converge endpoint.
Uses mocked LLM calls to avoid external API costs in CI.
"""

from __future__ import annotations

import copy
import hashlib
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from app.core.config import get_settings

_TEST_API_KEY = "pass"
# Precomputed sha256(_TEST_API_KEY) — the same literal digest already used by
# tests/test_api.py, tests/test_rate_limiter.py and tests/services/
# test_gate_registration.py. Stored rather than computed so no hashing call
# exists here at all: hashing a fixture key inline is what CodeQL flags as
# py/weak-sensitive-data-hashing (SHA-256 is not a computationally expensive
# password hash), and usedforsecurity=False does not suppress that query.
# app/core/auth.py still does the real comparison via hmac.compare_digest.
_TEST_API_KEY_HASH = "d74ff0ee8da3b9806b18c877dbf29bbde50b5bd8e4dad7a3a725000feb82e8f1"
AUTH = {"X-API-Key": _TEST_API_KEY}


@pytest.fixture(scope="module", autouse=True)
def _module_env() -> Iterator[None]:
    """Scope API_KEY_HASH to this module's tests.

    This was a module-level ``os.environ[...] =`` plus ``cache_clear()``, which
    pytest runs during collection and therefore for the whole session.
    ``patch.dict`` restores the caller's environment on teardown, and the
    settings cache is cleared on both sides so neither this module nor the next
    reads a ``Settings`` built from the other's environment.
    """
    with patch.dict(os.environ, {"API_KEY_HASH": _TEST_API_KEY_HASH}):
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


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
            "tier": "tier_1",
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


def _discovery_bytes(spec: dict) -> bytes:
    """Exact NEW fixture subject, matching API mapping serialization; never historical inference."""
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True).encode("utf-8")


def _native_discovery_spec() -> dict:
    """Complete NEW discovery input with the test additions not yet declared."""
    spec = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "domains/plasticos/spec.yaml").read_bytes()
    )
    node = spec["ontology"]["nodes"][0]
    discovered = {"capacity_tons_month", "tier", "certifications"}
    node["properties"] = [prop for prop in node["properties"] if prop["name"] not in discovered]
    node["enrichment_hints"]["priority_fields"] = [
        name for name in node["enrichment_hints"]["priority_fields"] if name not in discovered
    ]
    spec["inference_rules"] = [
        rule
        for rule in spec["inference_rules"]
        if all(
            member["field"] not in discovered
            for key in ("conditions", "outputs")
            for member in rule[key]
        )
    ]
    return spec


def _published_fixture_registry(root: Path, spec: dict):
    """Isolated reviewed-byte test input, not an operator approval/promotion."""
    import json

    import app.api.v1.converge as converge_module
    from app.engines.domain_yaml_reader import DomainYamlReader

    discovery = _discovery_bytes(spec)
    digest = hashlib.sha256(discovery).hexdigest()
    revision = root / "plasticos/revisions" / digest
    revision.mkdir(parents=True)
    (revision / "spec.yaml").write_bytes(discovery)
    (revision / "receipt.json").write_text(
        json.dumps(
            {
                "schema": "eie.domain-approval/v1",
                "tenant_id": "plasticos",
                "version": spec["version"],
                "sha256": digest,
                "reviewed_input_sha256": digest,
                "reviewed_by": "test-fixture-only",
                "approval_ref": "test-fixture-only",
                "reviewed_at": "2026-11-03T21:00:00+00:00",
                "previous_sha256": None,
                "approval_batches": [],
            }
        ),
        encoding="utf-8",
    )
    (root / "plasticos/current.json").write_text(
        json.dumps({"schema": "eie.domain-current/v1", "sha256": digest}), encoding="utf-8"
    )
    return converge_module.build_domain_spec_registry(
        DomainYamlReader(root, require_published=True)
    ), digest


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


def _held_state(domain: str = "plasticos", *, spec: dict | None = None, bound: bool = True):
    from app.engines.convergence.loop_state import LoopState, LoopStatus
    from app.models.field_confidence import FieldConfidence, FieldConfidenceMap, FieldSource

    fcm = FieldConfidenceMap()
    fcm.set(
        FieldConfidence(
            field_name="capacity_tons_month",
            value=5000.0,
            confidence=0.9,
            source=FieldSource.ENRICHMENT,
            pass_discovered=1,
        )
    )
    fcm.set(
        FieldConfidence(
            field_name="tier",
            value="tier_1",
            confidence=0.88,
            source=FieldSource.INFERENCE,
            pass_discovered=1,
        )
    )
    return LoopState(
        entity_id="ent-001",
        domain=domain,
        node_label="Facility" if bound else None,
        discovery_spec_sha256=hashlib.sha256(
            _discovery_bytes(spec or _list_shaped_spec(domain))
        ).hexdigest()
        if bound
        else None,
        ownership_contract_version=1 if bound else None,
        status=LoopStatus.HUMAN_HOLD,
        accumulated_fields={"capacity_tons_month": 5000.0, "tier": "tier_1"},
        accumulated_confidences=fcm,
        observed_field_sources={
            "capacity_tons_month": ["enrichment"],
            "tier": ["inference"],
        },
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
                        {
                            "node_label": "Facility",
                            "field_name": "capacity_tons_month",
                            "approved": True,
                        },
                        {
                            "node_label": "Facility",
                            "field_name": "tier",
                            "approved": False,
                        },
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
        assert _property_names(revised) == {"id", "capacity_tons_month"}
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
        ("capacity_tons_month", True),
        ("tier", False),
    }
    assert all(
        d["node_label"] == "Facility" and d["ownership_contract_version"] == 1
        for d in sent["decisions"]
    )
    assert all(d["discovery_spec_sha256"] == state.discovery_spec_sha256 for d in sent["decisions"])


@pytest.mark.asyncio
async def test_historical_unbound_run_refuses_without_restamping_or_writes(api_client):
    import app.api.v1.converge as converge_module

    state = _held_state(bound=False)
    before = state.model_dump()
    spec = _list_shaped_spec()
    original = _configure_for_approval(converge_module, state, spec)
    try:
        with patch.object(
            converge_module.pg_store, "persist_schema_proposal_batch", new_callable=AsyncMock
        ) as ledger:
            response = await api_client.post(
                f"/v1/converge/{state.run_id}/approve",
                json={
                    "decisions": [
                        {
                            "node_label": "Facility",
                            "field_name": "capacity_tons_month",
                            "approved": True,
                        }
                    ],
                    "reviewed_by": "test-fixture-only",
                },
                headers=AUTH,
            )
        assert response.status_code == 409
        assert response.json()["detail"] == "Legacy unbound discovery run cannot be approved"
        ledger.assert_not_awaited()
        assert state.model_dump() == before
        assert converge_module._domain_specs["plasticos"] is spec
        assert spec == _list_shaped_spec()
    finally:
        for name, value in original.items():
            setattr(converge_module, name, value)


@pytest.mark.asyncio
async def test_concurrent_approvals_for_one_domain_keep_both_field_sets(api_client, monkeypatch):
    """The lock serialises writes; the second old discovery refuses after the base changes."""
    import asyncio
    import uuid
    from types import SimpleNamespace

    import app.api.v1.converge as converge_module
    from app.engines.convergence.loop_state import LoopState, LoopStatus
    from app.models.field_confidence import FieldConfidence, FieldConfidenceMap, FieldSource
    from app.services.enrichment_profile import ProfileRegistry

    # Contended asyncio locks belong to this test's event loop, not a prior test's.
    monkeypatch.setattr(converge_module, "_domain_locks", {})

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
            node_label="Facility",
            discovery_spec_sha256=hashlib.sha256(_discovery_bytes(_list_shaped_spec())).hexdigest(),
            ownership_contract_version=1,
            status=LoopStatus.HUMAN_HOLD,
            accumulated_fields={field: value},
            accumulated_confidences=fcm,
            observed_field_sources={field: ["enrichment"]},
        )

    first, second = _held("capacity_tons_month", 5000.0), _held("certifications", "ISO")
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
        ) as ledger:
            responses = await asyncio.gather(
                api_client.post(
                    f"/v1/converge/{first.run_id}/approve",
                    json={
                        "decisions": [
                            {
                                "node_label": "Facility",
                                "field_name": "capacity_tons_month",
                                "approved": True,
                            }
                        ],
                        "reviewed_by": "test-fixture-only",
                    },
                    headers=AUTH,
                ),
                api_client.post(
                    f"/v1/converge/{second.run_id}/approve",
                    json={
                        "decisions": [
                            {
                                "node_label": "Facility",
                                "field_name": "certifications",
                                "approved": True,
                            }
                        ],
                        "reviewed_by": "test-fixture-only",
                    },
                    headers=AUTH,
                ),
            )
        # gather() does not fix which request takes the domain lock first, so the
        # winner is read from the answers: exactly one applies, the other is stale.
        statuses = [r.status_code for r in responses]
        assert sorted(statuses) == [200, 409], [r.text for r in responses]
        won = statuses.index(200)
        (winner, winner_field), (loser, _loser_field) = (
            ((first, "capacity_tons_month"), (second, "certifications"))
            if won == 0
            else ((second, "certifications"), (first, "capacity_tons_month"))
        )
        assert (
            responses[1 - won].json()["detail"]
            == "Discovery artifact changed; fresh owner-bound discovery required"
        )
        revised = converge_module._domain_specs["plasticos"]
        assert _property_names(revised) == {"id", winner_field}
        assert revised["version"] == "0.4.0-discovered", "only the nonstale approval applies"
        assert loser.status == LoopStatus.HUMAN_HOLD
        assert loser.discovery_spec_sha256 == winner.discovery_spec_sha256
        ledger.assert_awaited_once(), "stale second discovery cannot write an audit decision"
    finally:
        for name, value in original.items():
            setattr(converge_module, name, value)


@pytest.mark.asyncio
async def test_serialized_fresh_discoveries_keep_both_field_sets(api_client):
    """Both fields survive when the second NEW discovery explicitly sees the first revision."""
    import app.api.v1.converge as converge_module

    first = _held_for("capacity_tons_month", 5000.0)
    original = _configure_for_approval(converge_module, first, _list_shaped_spec())
    table = _ProposalTable()
    try:
        with patch.object(
            converge_module.pg_store, "persist_schema_proposal_batch", side_effect=table.persist
        ):
            response1 = await api_client.post(
                f"/v1/converge/{first.run_id}/approve",
                json={
                    "decisions": [
                        {
                            "node_label": "Facility",
                            "field_name": "capacity_tons_month",
                            "approved": True,
                        }
                    ],
                    "reviewed_by": "test-fixture-only",
                },
                headers=AUTH,
            )
            # This is a new synthetic discovery INPUT, never a restamp of the old run.
            fresh_spec = copy.deepcopy(converge_module._domain_specs["plasticos"])
            second = _held_for("certifications", "ISO", spec=fresh_spec)
            await converge_module._state_store.save(second)
            response2 = await api_client.post(
                f"/v1/converge/{second.run_id}/approve",
                json={
                    "decisions": [
                        {"node_label": "Facility", "field_name": "certifications", "approved": True}
                    ],
                    "reviewed_by": "test-fixture-only",
                },
                headers=AUTH,
            )
        assert [response1.status_code, response2.status_code] == [200, 200], [
            response1.text,
            response2.text,
        ]
        revised = converge_module._domain_specs["plasticos"]
        assert _property_names(revised) == {"id", "capacity_tons_month", "certifications"}
        assert revised["version"] == "0.5.0-discovered", "two fresh approvals, two version bumps"
        assert second.discovery_spec_sha256 != first.discovery_spec_sha256
        assert [row.discovery_spec_sha256 for row in table.rows] == [
            first.discovery_spec_sha256,
            second.discovery_spec_sha256,
        ]
    finally:
        for name, value in original.items():
            setattr(converge_module, name, value)


@pytest.mark.asyncio
async def test_concurrent_native_approvals_preserve_both_audits_without_activation(
    api_client, tmp_path, monkeypatch
):
    """Native concurrent decisions retain both audits; published original bytes stay authoritative."""
    import asyncio

    import app.api.v1.converge as converge_module
    from app.services.enrichment_profile import ProfileRegistry

    monkeypatch.setattr(converge_module, "_domain_locks", {})
    spec = _native_discovery_spec()
    registry, digest = _published_fixture_registry(tmp_path, spec)
    first = _held_for("capacity_tons_month", 5000.0, spec=spec)
    second = _held_for("certifications", "ISO", spec=spec)
    names = ("_state_store", "_profile_registry", "_domain_specs", "_kb_resolver", "_idem_store")
    original = {name: getattr(converge_module, name) for name in names}
    converge_module.configure(
        state_store=_RegistryStateStore(first, second),
        profile_registry=ProfileRegistry(),
        domain_specs=registry,
    )
    table = _ProposalTable()
    pointer_path = tmp_path / "plasticos/current.json"
    spec_path = tmp_path / "plasticos/revisions" / digest / "spec.yaml"
    receipt_path = spec_path.with_name("receipt.json")
    before = [path.read_bytes() for path in (pointer_path, spec_path, receipt_path)]

    async def slow_audit(**kwargs):
        await asyncio.sleep(0.05)
        return await table.persist(**kwargs)

    try:
        with patch.object(
            converge_module.pg_store, "persist_schema_proposal_batch", side_effect=slow_audit
        ) as ledger:
            responses = await asyncio.gather(
                *[
                    api_client.post(
                        f"/v1/converge/{state.run_id}/approve",
                        json={
                            "decisions": [
                                {"node_label": "Facility", "field_name": field, "approved": True}
                            ],
                            "reviewed_by": "test-fixture-only",
                        },
                        headers=AUTH,
                    )
                    for state, field in ((first, "capacity_tons_month"), (second, "certifications"))
                ]
            )
        assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
        assert ledger.await_count == 2
        assert {(r.node_label, r.field_name) for r in table.rows} == {
            ("Facility", "capacity_tons_month"),
            ("Facility", "certifications"),
        }
        assert all(
            r.discovery_spec_sha256 == digest and r.ownership_contract_version == 1
            for r in table.rows
        )
        assert all(r.reviewed_by == "test-fixture-only" for r in table.rows)
        assert all(
            r.json()["publication_status"] == "awaiting_operator_publication"
            and r.json()["new_version"] == "0.5.0"
            for r in responses
        )
        assert converge_module._domain_specs is registry
        assert registry["plasticos"] == spec
        assert _property_names(registry["plasticos"]) == _property_names(spec)
        assert [path.read_bytes() for path in (pointer_path, spec_path, receipt_path)] == before
        audit_before = copy.deepcopy([vars(row) for row in table.rows])
        with patch.object(
            converge_module.pg_store, "get_approved_schema_proposals", new_callable=AsyncMock
        ) as history:
            assert await converge_module.hydrate_approved_revisions() == {}
            history.assert_not_awaited()
        assert registry["plasticos"] == spec
        assert [vars(row) for row in table.rows] == audit_before
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
            json={
                "decisions": [
                    {
                        "node_label": "Facility",
                        "field_name": "capacity_tons_month",
                        "approved": True,
                    }
                ],
                "reviewed_by": "test-fixture-only",
            },
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
        node_label=extra.get("node_label"),
        discovery_spec_sha256=extra.get("discovery_spec_sha256"),
        ownership_contract_version=extra.get("ownership_contract_version"),
        reviewed_by="test-fixture-only",
        reviewed_at=datetime(2026, 11, 3, 21, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_hydrate_replays_approved_rows_onto_the_registry():
    """Historical unbound startup evidence refuses safely and retains its audit payload."""
    import app.api.v1.converge as converge_module
    from app.services.enrichment_profile import ProfileRegistry

    names = ("_state_store", "_profile_registry", "_domain_specs", "_kb_resolver", "_idem_store")
    original = {name: getattr(converge_module, name) for name in names}
    rows = [
        _approved_row("capacity_tons_month", "float", "batch-1"),
        _approved_row("tier", "string", "batch-2", proposed_gate="categorical"),
    ]
    before = copy.deepcopy([vars(row) for row in rows])
    converge_module.configure(
        state_store=_RegistryStateStore(),
        profile_registry=ProfileRegistry(),
        domain_specs={"plasticos": _list_shaped_spec()},
    )
    try:
        with (
            patch.object(
                converge_module.pg_store,
                "get_approved_schema_proposals",
                new=AsyncMock(return_value=rows),
            ) as mock_get,
            pytest.raises(ValueError, match="^Schema proposal is legacy-unbound$"),
        ):
            await converge_module.hydrate_approved_revisions()
        mock_get.assert_awaited_once_with(tenant_id="plasticos", domain="plasticos")
        revised = converge_module._domain_specs["plasticos"]
        assert revised == _list_shaped_spec()
        assert [vars(row) for row in rows] == before
        assert rows[1].proposed_gate == "categorical", (
            "historical ranking payload retained, never applied"
        )

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


def test_explicit_bound_replay_keeps_batch_fields_associations_and_historical_payload():
    """Two retained exact discovery artifacts replay with two bumps, never ranking controls."""
    import app.api.v1.converge as converge_module

    base = _native_discovery_spec()
    first_bytes = _discovery_bytes(base)
    first_digest = hashlib.sha256(first_bytes).hexdigest()
    second_discovery = copy.deepcopy(base)
    second_discovery["ontology"]["nodes"][0]["properties"].append(
        {"name": "capacity_tons_month", "type": "float", "source": "enrichment"}
    )
    second_discovery["version"] = "0.5.0-discovered"
    second_bytes = _discovery_bytes(second_discovery)
    second_digest = hashlib.sha256(second_bytes).hexdigest()
    rows = [
        _approved_row(
            "capacity_tons_month",
            "float",
            "batch-1",
            node_label="Facility",
            discovery_spec_sha256=first_digest,
            ownership_contract_version=1,
        ),
        _approved_row(
            "tier",
            "string",
            "batch-2",
            node_label="Facility",
            discovery_spec_sha256=second_digest,
            ownership_contract_version=1,
            proposed_gate="categorical",
            proposed_scoring_dimension="numeric_distance",
        ),
    ]
    before = copy.deepcopy([vars(row) for row in rows])
    replayed = converge_module.apply_approved_records(
        base, rows, discovery_specs={first_digest: first_bytes, second_digest: second_bytes}
    )
    assert _property_names(replayed) == _property_names(base) | {
        "capacity_tons_month",
        "tier",
    }
    assert replayed["version"] == "0.7.0-discovered"
    assert replayed["sync"] == base["sync"]
    assert replayed["semantic_registry"] == base["semantic_registry"]
    assert replayed["inference_rules"] == base["inference_rules"]
    assert replayed["gates"] == [] and replayed["scoring"]["dimensions"] == []
    assert [vars(row) for row in rows] == before
    assert "capacity_tons_month" not in _property_names(base) and "tier" not in _property_names(
        base
    )


def test_apply_serves_list_shaped_and_dict_shaped_ontologies():
    """schema_proposer.apply writes into whichever shape the spec declares."""
    from app.engines.convergence.schema_proposer import (
        ApprovalDecision,
        FieldProposal,
        SchemaProposalSet,
        apply,
    )

    proposal = SchemaProposalSet(
        proposed_fields=[FieldProposal(field_name="capacity_tons_month", field_type="float")],
        version_bump="0.4.0-discovered",
    )
    decisions = [ApprovalDecision(field_name="capacity_tons_month", approved=True)]

    listed_spec = _list_shaped_spec()
    digest = hashlib.sha256(_discovery_bytes(listed_spec)).hexdigest()
    native_proposal = SchemaProposalSet(
        domain="plasticos",
        tenant_id="plasticos",
        discovery_spec_sha256=digest,
        ownership_contract_version=1,
        proposed_fields=[
            FieldProposal(
                field_name="capacity_tons_month",
                field_type="float",
                node_label="Facility",
                discovery_spec_sha256=digest,
                ownership_contract_version=1,
            )
        ],
        version_bump="0.4.0-discovered",
    )
    listed = apply(
        listed_spec,
        [ApprovalDecision(node_label="Facility", field_name="capacity_tons_month", approved=True)],
        native_proposal,
        source_sha256=digest,
    )
    assert {"name": "capacity_tons_month", "type": "float", "source": "enrichment"} in listed[
        "ontology"
    ]["nodes"][0]["properties"]

    dict_spec = {"version": "0.3.0", "ontology": {"nodes": {"Facility": {"properties": {}}}}}
    dicted = apply(dict_spec, decisions, proposal)
    assert dicted["ontology"]["nodes"]["Facility"]["properties"]["capacity_tons_month"] == {
        "type": "float",
        "source": "enrichment",
    }


# ── F-247-2: a batch that applies nothing publishes no revision ──────────────


def _held_for(field: str, value, *, spec: dict | None = None):
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
        node_label="Facility",
        discovery_spec_sha256=hashlib.sha256(
            _discovery_bytes(spec or _list_shaped_spec())
        ).hexdigest(),
        ownership_contract_version=1,
        status=LoopStatus.HUMAN_HOLD,
        accumulated_fields={field: value},
        accumulated_confidences=fcm,
        observed_field_sources={field: ["enrichment"]},
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
                reviewed_by=kwargs["reviewed_by"],
                reviewed_at=datetime(2026, 11, 3, 21, 0, tzinfo=UTC),
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
    """All-rejected/unknown pairs bump nothing; explicit bound replay matches applied live."""
    import copy

    import app.api.v1.converge as converge_module
    from app.services.enrichment_profile import ProfileRegistry

    base = _native_discovery_spec()
    discovery = _discovery_bytes(base)
    digest = hashlib.sha256(discovery).hexdigest()
    rejected = _held_for("capacity_tons_month", 5000.0, spec=base)
    unproposed = _held_for("certifications", "ISO", spec=base)
    applied = _held_for("tier", "tier_1", spec=base)
    table = _ProposalTable()
    names = ("_state_store", "_profile_registry", "_domain_specs", "_kb_resolver", "_idem_store")
    original = {name: getattr(converge_module, name) for name in names}
    converge_module.configure(
        state_store=_RegistryStateStore(rejected, unproposed, applied),
        profile_registry=ProfileRegistry(),
        domain_specs={"plasticos": base},
    )
    sequence = [
        (
            rejected,
            [{"node_label": "Facility", "field_name": "capacity_tons_month", "approved": False}],
        ),
        (
            unproposed,
            [{"node_label": "Facility", "field_name": "not_a_proposed_field", "approved": True}],
        ),
        (applied, [{"node_label": "Facility", "field_name": "tier", "approved": True}]),
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
                    json={"decisions": decisions, "reviewed_by": "test-fixture-only"},
                    headers=AUTH,
                )
                for state, decisions in sequence
            ]
            live = copy.deepcopy(converge_module._domain_specs["plasticos"])
            # Restart: the file-backed revision, then the approved rows replayed.
            converge_module.configure(
                state_store=_RegistryStateStore(),
                profile_registry=ProfileRegistry(),
                domain_specs={"plasticos": copy.deepcopy(base)},
            )
            # Automatic legacy replay has no retained-byte inputs and must refuse.
            with pytest.raises(
                ValueError, match="Retained replay requires the recorded discovery artifact"
            ):
                await converge_module.hydrate_approved_revisions()
            assert converge_module._domain_specs["plasticos"] == base
            audit_before = copy.deepcopy([vars(row) for row in table.rows])
            replayed = converge_module.apply_approved_records(
                base,
                await table.approved(tenant_id="plasticos", domain="plasticos"),
                discovery_specs={digest: discovery},
            )
            assert [vars(row) for row in table.rows] == audit_before
    finally:
        for name, value in original.items():
            setattr(converge_module, name, value)

    assert [r.status_code for r in responses] == [200, 422, 200], [r.text for r in responses]
    assert responses[1].json()["detail"] == "Approval decision names an unproposed owner/field pair"
    bodies = [responses[0].json(), responses[2].json()]
    assert [b["approved"] for b in bodies] == [0, 1], "applied counts, not decided counts"
    assert [b["new_version"] for b in bodies] == ["0.5.0", "0.6.0-discovered"]
    assert all(b["unapplied"] == [] for b in bodies)
    assert len(table.rows) == 2 and all(
        row.field_name != "not_a_proposed_field" for row in table.rows
    )
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
