"""Domain identity registry contract (docs/contracts/DOMAIN_IDENTITY_REGISTRY.md).

tenant id == domain spec id == Neo4j database name. The registry is built from
`domains/`, readiness is 503 while it is empty or failed, approvals are
persisted in one transaction and replayed at startup, and an unknown domain is
404 on both scan routes.

`app.main` reaches the Perplexity SDK transitively (main -> orchestration_layer
-> handlers -> enrichment_orchestrator -> perplexity_client). The constitution
gate runs `tests/contracts/` without the provider runtime, so the readiness pin
imports it lazily behind `requires_app_runtime`; everything else here needs no
runtime at all. See tests/contracts/test_canonical_converge_runtime_contract.py
for the same arrangement.

L9_META:
  tier: 2
  domain: identity
  authority: operator decision 2026-10-22 (plan dac62dc3)
  pr_class: app_code + tier2_test
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.v1.converge as converge
import app.api.v1.discover as discover
from app.core.auth import verify_api_key
from app.engines import domain_yaml_reader
from app.engines.convergence.schema_proposer import (
    ApprovalDecision,
    FieldProposal,
    SchemaProposalSet,
)
from app.engines.domain_yaml_reader import DomainYamlReader, is_legal_tenant_id
from app.services import pg_store

_HAS_PROVIDER_RUNTIME = importlib.util.find_spec("perplexity") is not None
requires_app_runtime = pytest.mark.skipif(
    not _HAS_PROVIDER_RUNTIME,
    reason="app.main needs the Perplexity SDK; not installed in the contract gate env",
)


def _spec(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "version": "0.3.0",
        "ontology": {"nodes": [{"label": "Facility", "properties": [{"name": "id"}]}]},
    }


def _write(root: Path, tenant: str, body: dict[str, Any] | str) -> None:
    (root / tenant).mkdir(parents=True)
    text = body if isinstance(body, str) else yaml.safe_dump(body)
    (root / tenant / "spec.yaml").write_text(text, encoding="utf-8")


# ── 1. Identity grammar ──────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("tenant", ["plasticos", "acme.eu", "a-b.c1", "abc", "a" + "b" * 62])
def test_legal_tenant_ids(tenant: str) -> None:
    assert is_legal_tenant_id(tenant)


@pytest.mark.unit
@pytest.mark.parametrize(
    "tenant",
    [
        "Plasticos",
        "ab",
        "system-x",
        "systemdb",
        "acme_eu",
        "-abc",
        "1abc",
        "a" + "b" * 63,
        "",
        None,
    ],
)
def test_illegal_tenant_ids(tenant: object) -> None:
    assert not is_legal_tenant_id(tenant)


@pytest.mark.unit
def test_reader_and_registry_share_one_grammar(tmp_path: Path) -> None:
    """One function, imported by the registry; no second regex to drift."""
    assert converge.is_legal_tenant_id is domain_yaml_reader.is_legal_tenant_id
    assert domain_yaml_reader.TENANT_ID_PATTERN.pattern == r"^[a-z][a-z0-9.-]{2,62}$"
    reader = DomainYamlReader(tmp_path)
    for refused in ("Plasticos", "../plasticos", "res_partner", "systemdb"):
        with pytest.raises(ValueError, match="Invalid domain id"):
            reader.load(refused)


# ── 2. Registry ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_registry_is_built_from_domains_root_keyed_by_tenant_id(tmp_path: Path) -> None:
    root = tmp_path / "domains"
    _write(root, "plasticos", _spec("plasticos"))
    _write(root, "acme.eu", _spec("acme.eu"))  # dotted: legal database name
    _write(root, "Metals", _spec("Metals"))  # uppercase
    _write(root, "systemwide", _spec("systemwide"))  # reserved prefix
    _write(root, "renamed", _spec("something-else"))  # declared id != directory
    _write(root, "broken", "ontology: [:::")  # malformed YAML
    _write(root, "listroot", "- just\n- a list\n")  # wrong shape
    _write(root, "mapnodes", {"name": "mapnodes", "ontology": {"nodes": {"Facility": {}}}})

    registry = converge.build_domain_spec_registry(DomainYamlReader(root))

    assert sorted(registry) == ["acme.eu", "plasticos"]
    assert DomainYamlReader(root).load("acme.eu").domain_id == "acme.eu"


@pytest.mark.unit
def test_checked_in_domains_root_registers_plasticos() -> None:
    root = Path(__file__).resolve().parents[2] / "domains"
    assert "plasticos" in converge.build_domain_spec_registry(DomainYamlReader(root))


# ── 3. Unknown domain: 404 on both scan routes ───────────────────────────────


@pytest.mark.unit
def test_unknown_domain_is_404_on_both_scan_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FastAPI()
    api.include_router(converge.router)
    api.include_router(discover.router)
    api.dependency_overrides[verify_api_key] = lambda: None
    monkeypatch.setattr(converge, "_domain_specs", {"plasticos": _spec("plasticos")})
    client = TestClient(api)

    v1 = client.post("/v1/scan", json={"fields": [], "domain": "nope"})
    api_v1 = client.post(
        "/api/v1/scan", json={"fields": [], "domain": "nope", "tenant_id": "plasticos"}
    )
    assert (v1.status_code, api_v1.status_code) == (404, 404)
    assert v1.json()["detail"] == api_v1.json()["detail"]
    assert "nope" in v1.json()["detail"]


# ── 4. Approvals: one transaction, replayed at startup ───────────────────────


class _FakeTx:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeTx:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self._session.committed = True
        return False


class _FakeSession:
    """What `pg_store.get_session` drives: one `begin()`, commit on clean exit."""

    def __init__(self, opened: list[_FakeSession], fail_on_flush: bool) -> None:
        self.fail_on_flush = fail_on_flush
        self.added: list[Any] = []
        self.committed = False
        self.rolled_back = False
        opened.append(self)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    def begin(self) -> _FakeTx:
        return _FakeTx(self)

    async def execute(self, _stmt: Any) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    def add(self, record: Any) -> None:
        self.added.append(record)

    async def flush(self) -> None:
        if self.fail_on_flush:
            raise RuntimeError("disk full")

    async def rollback(self) -> None:
        self.rolled_back = True


def _decisions() -> list[dict[str, Any]]:
    base = {"field_type": "float", "source": "enrichment", "fill_rate": 1.0, "avg_confidence": 0.9}
    return [
        {"field_name": "annual_capacity_mt", "approved": True, **base},
        {"field_name": "facility_tier", "approved": False, **base},
        {"field_name": "certifications", "approved": True, **base},
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_approval_batch_is_one_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[_FakeSession] = []
    monkeypatch.setattr(pg_store, "_session_factory", lambda: _FakeSession(opened, False))

    records = await pg_store.persist_schema_proposal_batch(
        tenant_id="plasticos",
        domain="plasticos",
        batch_run_id="run-1",
        reviewed_by="reviewer",
        decisions=_decisions(),
    )

    assert len(opened) == 1, "one session for the whole batch"
    assert opened[0].committed is True
    assert [r.approval_status for r in records] == ["approved", "rejected", "approved"]
    assert all(r.tenant_id == r.domain == "plasticos" for r in records)
    assert all(r.batch_run_id == "run-1" and r.reviewed_by == "reviewer" for r in records)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failing_row_leaves_zero_durable_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[_FakeSession] = []
    monkeypatch.setattr(pg_store, "_session_factory", lambda: _FakeSession(opened, True))

    with pytest.raises(RuntimeError, match="disk full"):
        await pg_store.persist_schema_proposal_batch(
            tenant_id="plasticos",
            domain="plasticos",
            batch_run_id="run-1",
            reviewed_by="reviewer",
            decisions=_decisions(),
        )

    assert len(opened) == 1
    assert opened[0].committed is False, "nothing of the batch is durable"
    assert opened[0].rolled_back is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_seam_persists_every_decided_field_through_the_batch_operation() -> None:
    proposal_set = SchemaProposalSet(
        proposed_fields=[
            FieldProposal(field_name="annual_capacity_mt", field_type="float"),
            FieldProposal(field_name="facility_tier", field_type="string"),
            FieldProposal(field_name="undecided", field_type="string"),
        ],
        version_bump="0.4.0-discovered",
    )
    decisions = [
        ApprovalDecision(field_name="annual_capacity_mt", approved=True),
        ApprovalDecision(field_name="facility_tier", approved=False),
    ]
    batch = AsyncMock(side_effect=lambda **kw: [SimpleNamespace(**d) for d in kw["decisions"]])
    with patch.object(converge.pg_store, "persist_schema_proposal_batch", new=batch):
        written = await converge.persist_approved_revision(
            tenant_id="plasticos",
            domain="plasticos",
            batch_run_id="run-1",
            proposal_set=proposal_set,
            decisions=decisions,
            reviewed_by="reviewer",
        )
    assert written == 2
    batch.assert_awaited_once()
    sent = batch.await_args.kwargs
    assert sent["tenant_id"] == sent["domain"] == "plasticos"
    assert [(d["field_name"], d["approved"]) for d in sent["decisions"]] == [
        ("annual_capacity_mt", True),
        ("facility_tier", False),
    ]


@pytest.mark.unit
def test_startup_replays_approved_rows_batch_by_batch() -> None:
    row = SimpleNamespace(
        tenant_id="plasticos",
        domain="plasticos",
        batch_run_id="run-1",
        field_name="annual_capacity_mt",
        field_type="float",
        source="enrichment",
        fill_rate=1.0,
        avg_confidence=0.9,
        sample_values=[],
        value_distribution={},
        proposed_gate=None,
        proposed_scoring_dimension=None,
        approval_status="approved",
    )
    revised = converge.apply_approved_records(_spec("plasticos"), [row])
    names = {p["name"] for p in revised["ontology"]["nodes"][0]["properties"]}
    assert names == {"id", "annual_capacity_mt"}
    assert revised["version"] == "0.4.0-discovered"


# ── 5. Readiness reflects the registry ──────────────────────────────────────


@requires_app_runtime
@pytest.mark.parametrize(
    ("state", "expected"),
    [("loaded", 200), ("not_attempted", 200), ("empty", 503), ("failed", 503)],
)
def test_readiness_is_503_while_registry_is_empty_or_failed(
    monkeypatch: pytest.MonkeyPatch, state: str, expected: int
) -> None:
    from app import main as main_module
    from app.core.config import get_settings

    assert frozenset({"empty", "failed"}) == main_module.DOMAIN_REGISTRY_DEGRADED
    monkeypatch.setenv("GATE_REGISTRATION_ENABLED", "false")
    monkeypatch.setattr(main_module, "_domain_registry_state", state)
    get_settings.cache_clear()
    try:
        resp = TestClient(main_module.app).get(main_module.READINESS_ENDPOINT)
    finally:
        get_settings.cache_clear()
    assert resp.status_code == expected
    assert resp.json()["ready"] is (expected == 200)
