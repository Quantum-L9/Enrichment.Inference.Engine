"""Domain identity registry contract (docs/contracts/DOMAIN_IDENTITY_REGISTRY.md).

tenant id == domain spec id == Neo4j database name. The registry is built from
`domains/`, readiness is 503 while it is empty or failed, approvals are
persisted in one transaction and replayed at startup, and an unknown domain is
404 on both scan routes.

Two runtimes this file does NOT assume, because the constitution gate
(`.github/workflows/l9-constitution-gate.yml`, `pytest tests/contracts/ -m "unit
and not enforcement"`) installs fastapi / httpx / pydantic / pyyaml / structlog /
redis and constellation-node-sdk only:

* the DB runtime — `app.api.v1.converge` and `app.api.v1.discover` import
  `app.services.pg_store`, which imports SQLAlchemy (`pg_models`). The tests that
  need those modules import them lazily through `_converge()`, `_discover()`,
  `_pg_store()` and `_schema_proposer()` and carry `requires_db_runtime`, so they
  are reported as skipped in the gate instead of breaking collection of the
  whole module. Removal trigger: the gate installing the project's DB runtime
  (`sqlalchemy`), after which the condition is always true;
* the provider runtime — `app.main` reaches the Perplexity SDK (main ->
  orchestration_layer -> handlers -> enrichment_orchestrator ->
  perplexity_client). The readiness pin imports it lazily behind
  `requires_app_runtime`, the arrangement
  tests/contracts/test_canonical_converge_runtime_contract.py uses.

The grammar and reader pins (`is_legal_tenant_id`, `DomainYamlReader`) import
only `app.engines.domain_yaml_reader` (yaml + structlog) and run in the gate.

L9_META:
  tier: 2
  domain: identity
  authority: operator decision 2026-10-22 (plan dac62dc3)
  pr_class: app_code + tier2_test
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from app.engines import domain_yaml_reader
from app.engines.domain_yaml_reader import DomainYamlReader, is_legal_tenant_id

_HAS_DB_RUNTIME = importlib.util.find_spec("sqlalchemy") is not None
requires_db_runtime = pytest.mark.skipif(
    not _HAS_DB_RUNTIME,
    reason="app.api.v1.converge / app.services.pg_store need SQLAlchemy; "
    "not installed in the contract gate env",
)

_HAS_PROVIDER_RUNTIME = importlib.util.find_spec("perplexity") is not None
requires_app_runtime = pytest.mark.skipif(
    not _HAS_PROVIDER_RUNTIME,
    reason="app.main needs the Perplexity SDK; not installed in the contract gate env",
)


def _converge():
    """Lazy: reaches SQLAlchemy through app.services.pg_store. See the note above."""
    return importlib.import_module("app.api.v1.converge")


def _discover():
    return importlib.import_module("app.api.v1.discover")


def _pg_store():
    return importlib.import_module("app.services.pg_store")


def _schema_proposer():
    """Lazy: the `app.engines.convergence` package init imports the loop-state store."""
    return importlib.import_module("app.engines.convergence.schema_proposer")


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


# ── 1. Identity grammar (no runtime) ─────────────────────────────────────────


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
def test_grammar_is_the_neo4j_database_name_grammar() -> None:
    assert domain_yaml_reader.TENANT_ID_PATTERN.pattern == r"^[a-z][a-z0-9.-]{2,62}$"
    assert domain_yaml_reader.TENANT_ID_RESERVED_PREFIX == "system"


@pytest.mark.unit
def test_reader_enforces_the_grammar_on_every_id_that_reaches_the_filesystem(
    tmp_path: Path,
) -> None:
    reader = DomainYamlReader(tmp_path)
    for refused in (
        "Plasticos",
        "../plasticos",
        "plasticos/../plasticos",
        "res_partner",
        "systemdb",
    ):
        with pytest.raises(ValueError, match="Invalid domain id"):
            reader.load(refused)


@pytest.mark.unit
def test_reader_loads_a_dotted_tenant(tmp_path: Path) -> None:
    """A dotted id is a legal Neo4j database name and resolves to its own directory."""
    root = tmp_path / "domains"
    _write(root, "acme.eu", _spec("acme.eu"))
    config = DomainYamlReader(root).load("acme.eu")
    assert config.domain_id == "acme.eu"
    assert "Facility" in config.node_schemas


@pytest.mark.unit
@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"name": "plasticos", "version": "0.3.0"}, ("plasticos", "0.3.0")),
        ({"domain": "acme"}, ("acme", None)),
        ({"domain": {"id": "acme", "name": "Acme", "version": "8.0.0"}}, ("acme", "8.0.0")),
        ({"domain_identity": {"name": "acme", "version": "1.0.0"}}, ("acme", "1.0.0")),
        # An approval bumps the top-level version; it wins over a stale block version.
        (
            {"domain": {"id": "acme", "version": "8.0.0"}, "version": "8.1.0-discovered"},
            ("acme", "8.1.0-discovered"),
        ),
        ({"metadata": {"template": {"id": "plastics", "version": "2.0.0"}}}, (None, None)),
        ([], (None, None)),
    ],
)
def test_one_spec_identity_reader(spec: Any, expected: tuple[str | None, str | None]) -> None:
    """Registry, reader and scan share `declared_spec_identity`; template provenance is not identity."""
    assert domain_yaml_reader.declared_spec_identity(spec) == expected


@pytest.mark.unit
def test_reader_reports_the_spec_identity_version(tmp_path: Path) -> None:
    root = tmp_path / "domains"
    _write(root, "plasticos", _spec("plasticos"))
    _write(root, "acme", {"domain": "acme", "version": "2.0.0", "ontology": {"nodes": []}})
    reader = DomainYamlReader(root)
    assert reader.load("plasticos").version == "0.3.0"
    assert reader.load("acme").version == "2.0.0"


# ── 2. Registry (DB runtime: converge imports pg_store) ──────────────────────


@requires_db_runtime
@pytest.mark.unit
def test_reader_and_registry_share_one_grammar() -> None:
    """One function, imported by the registry; no second regex to drift."""
    assert _converge().is_legal_tenant_id is domain_yaml_reader.is_legal_tenant_id


@requires_db_runtime
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

    registry = _converge().build_domain_spec_registry(DomainYamlReader(root))

    assert sorted(registry) == ["acme.eu", "plasticos"]


@requires_db_runtime
@pytest.mark.unit
def test_checked_in_domains_root_registers_plasticos() -> None:
    root = Path(__file__).resolve().parents[2] / "domains"
    assert "plasticos" in _converge().build_domain_spec_registry(DomainYamlReader(root))


@requires_db_runtime
@pytest.mark.unit
def test_registry_admits_only_directory_specs(tmp_path: Path) -> None:
    """`domains/<tenant>/spec.yaml` registers; a flat file is refused and named, never a tenant."""
    converge = _converge()
    root = tmp_path / "domains"
    _write(root, "acme", _spec("acme"))
    (root / "acme.yaml").write_text(yaml.safe_dump(_spec("acme")), encoding="utf-8")
    (root / "acme.bak.yaml").write_text(yaml.safe_dump({"version": "0.0.1"}), encoding="utf-8")

    with patch.object(converge, "logger") as log:
        registry = converge.build_domain_spec_registry(DomainYamlReader(root))

    assert sorted(registry) == ["acme"]
    refused = [
        call.kwargs.get("file")
        for call in log.warning.call_args_list
        if call.args == ("domain_registry_refused_flat_spec",)
    ]
    assert sorted(refused) == ["acme.bak.yaml", "acme.yaml"]


@requires_db_runtime
@pytest.mark.unit
def test_registry_refusal_names_the_one_grammar() -> None:
    """The refusal log cites TENANT_ID_PATTERN; the API module restates no grammar."""
    source = Path(_converge().__file__).read_text(encoding="utf-8")
    assert domain_yaml_reader.TENANT_ID_PATTERN.pattern not in source


# ── 3. Unknown domain: 404 on both scan routes ───────────────────────────────


@requires_db_runtime
@pytest.mark.unit
def test_unknown_domain_is_404_on_both_scan_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.auth import verify_api_key

    converge, discover = _converge(), _discover()
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


def _direct_client(monkeypatch: pytest.MonkeyPatch, environment: str):
    """Both routers, auth satisfied, the registry holding plasticos, in `environment`."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.auth import verify_api_key
    from app.core.config import get_settings

    converge, discover = _converge(), _discover()
    api = FastAPI()
    api.include_router(converge.router)
    api.include_router(discover.router)
    api.dependency_overrides[verify_api_key] = lambda: None
    monkeypatch.setattr(converge, "_domain_specs", {"plasticos": _spec("plasticos")})
    monkeypatch.setenv("L9_ENVIRONMENT", environment)
    get_settings.cache_clear()
    return TestClient(api)


_DIRECT_STATUS = {"local": 200, "dev": 200, "test": 200, "staging": 403, "prod": 403}


@requires_db_runtime
@pytest.mark.unit
@pytest.mark.parametrize("environment", sorted(_DIRECT_STATUS))
def test_both_scan_routes_share_one_tenant_posture(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    """F-247-4 / F-247-5: one posture, one status per environment on both scan routes.

    A single deployment key binds its holder to no tenant, so a direct scan of a
    tenant's spec is served only where the runtime is unsigned (local/dev/test)
    and refused (403) everywhere else.
    """
    from app.core.config import get_settings

    try:
        client = _direct_client(monkeypatch, environment)
        v1 = client.post("/v1/scan", json={"fields": [], "domain": "plasticos"})
        api_v1 = client.post(
            "/api/v1/scan", json={"fields": [], "domain": "plasticos", "tenant_id": "plasticos"}
        )
    finally:
        get_settings.cache_clear()
    assert v1.status_code == api_v1.status_code
    assert v1.status_code == _DIRECT_STATUS[environment]
    if v1.status_code == 403:
        # One owner (auth.ensure_direct_tenant_read_allowed): the same refusal on both.
        assert v1.json() == api_v1.json()


@requires_db_runtime
@pytest.mark.unit
@pytest.mark.parametrize("environment", ["staging", "prod", "production"])
def test_tenant_scoped_converge_routes_are_refused_outside_unsigned_runtimes(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    """Approval and proposals are tenant-scoped too: refused before any run or spec is read."""
    from app.core.config import get_settings

    try:
        client = _direct_client(monkeypatch, environment)
        approve = client.post(
            "/v1/converge/00000000-0000-0000-0000-000000000000/approve",
            json={
                "decisions": [{"field_name": "capacity_tons_month", "approved": True}],
                "reviewed_by": "test-fixture-only",
            },
        )
        proposals = client.get("/v1/converge/proposals/plasticos")
    finally:
        get_settings.cache_clear()
    assert approve.status_code == 403
    assert proposals.status_code == 403


# ── 4. Approvals: one transaction, replayed at startup ───────────────────────


class _FakeTx:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeTx:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self._session.committed = True
            self._session.durable = list(self._session.added)
        return False


class _FakeSession:
    """Insert DO NOTHING + locked reload, under the real get_session transaction."""

    def __init__(self, opened: list[_FakeSession], fail_on_flush: bool) -> None:
        self.fail_on_flush = fail_on_flush
        self.added: list[Any] = []
        self.durable: list[Any] = []
        self.statements: list[Any] = []
        self.flushes = 0
        self.begins = 0
        self.committed = False
        self.rolled_back = False
        opened.append(self)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    def begin(self) -> _FakeTx:
        self.begins += 1
        return _FakeTx(self)

    async def execute(self, stmt: Any) -> Any:
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.sql.dml import Insert

        from app.services.pg_models import SchemaProposalRecord

        self.statements.append(stmt)
        compiled = stmt.compile(dialect=postgresql.dialect())
        identity = ("tenant_id", "domain", "batch_run_id", "node_label", "field_name")
        if isinstance(stmt, Insert):
            assert "DO NOTHING" in str(compiled) and "DO UPDATE" not in str(compiled)
            values = dict(compiled.params)
            key = tuple(values[name] for name in identity)
            if not any(tuple(getattr(row, name) for name in identity) == key for row in self.added):
                record = SchemaProposalRecord(**values)
                record.id = uuid.uuid4()
                record.created_at = datetime(2026, 1, 1, tzinfo=UTC)
                self.added.append(record)
            return SimpleNamespace(scalar_one_or_none=lambda: None)
        assert "FOR UPDATE" in str(compiled)
        assert stmt.get_execution_options()["populate_existing"] is True
        key = tuple(compiled.params[f"{name}_1"] for name in identity)
        row = next(
            (r for r in self.added if tuple(getattr(r, name) for name in identity) == key),
            None,
        )
        return SimpleNamespace(scalar_one_or_none=lambda: row)

    async def flush(self) -> None:
        self.flushes += 1
        if self.fail_on_flush:
            assert len(self.added) >= 2, "earlier rows are staged before the final flush fails"
            raise RuntimeError("disk full")

    async def rollback(self) -> None:
        self.rolled_back = True
        self.added.clear()
        self.durable.clear()


def _decisions() -> list[dict[str, Any]]:
    # Intentional NEW discovery input, not a restamp of historical evidence.
    discovery = yaml.safe_dump(_spec("plasticos"), sort_keys=False, allow_unicode=True).encode(
        "utf-8"
    )
    base = {
        "field_type": "float",
        "source": "enrichment",
        "fill_rate": 1.0,
        "avg_confidence": 0.9,
        "node_label": "Facility",
        "discovery_spec_sha256": hashlib.sha256(discovery).hexdigest(),
        "ownership_contract_version": 1,
    }
    return [
        {"field_name": "capacity_tons_month", "approved": True, **base},
        {"field_name": "tier", "approved": False, **base},
        {"field_name": "certifications", "approved": True, **base},
    ]


@requires_db_runtime
@pytest.mark.unit
@pytest.mark.asyncio
async def test_approval_batch_is_one_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    pg_store = _pg_store()
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
    assert opened[0].begins == 1
    assert len(opened[0].durable) == 3
    assert len(opened[0].statements) == 6, "each insert has a locked reload"
    assert [r.approval_status for r in records] == ["approved", "rejected", "approved"]
    assert all(r.tenant_id == r.domain == "plasticos" for r in records)
    assert all(r.batch_run_id == "run-1" and r.reviewed_by == "reviewer" for r in records)
    assert all(r.node_label == "Facility" and r.ownership_contract_version == 1 for r in records)
    assert all(r.discovery_spec_sha256 == _decisions()[0]["discovery_spec_sha256"] for r in records)


@requires_db_runtime
@pytest.mark.unit
@pytest.mark.asyncio
async def test_failing_row_leaves_zero_durable_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    pg_store = _pg_store()
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
    assert opened[0].begins == 1
    assert opened[0].flushes == 1, "the batch flush fails after its rows were staged"
    assert opened[0].durable == opened[0].added == []


@requires_db_runtime
@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_unbound_batch_refuses_before_session(monkeypatch: pytest.MonkeyPatch) -> None:
    pg_store = _pg_store()
    opened: list[_FakeSession] = []
    monkeypatch.setattr(pg_store, "_session_factory", lambda: _FakeSession(opened, False))
    decisions = _decisions()
    for decision in decisions:
        for key in ("node_label", "discovery_spec_sha256", "ownership_contract_version"):
            decision.pop(key)
    before = copy.deepcopy(decisions)
    with pytest.raises(ValueError, match="^Schema proposal is legacy-unbound$"):
        await pg_store.persist_schema_proposal_batch(
            tenant_id="plasticos",
            domain="plasticos",
            batch_run_id="run-1",
            reviewed_by="reviewer",
            decisions=decisions,
        )
    assert opened == [], "invalid input opens no transaction and writes no rows"
    assert decisions == before


@requires_db_runtime
@pytest.mark.unit
@pytest.mark.asyncio
async def test_api_seam_persists_every_decided_field_through_the_batch_operation() -> None:
    converge, proposer = _converge(), _schema_proposer()
    binding = {
        key: _decisions()[0][key]
        for key in ("node_label", "discovery_spec_sha256", "ownership_contract_version")
    }
    proposal_set = proposer.SchemaProposalSet(
        domain="plasticos",
        tenant_id="plasticos",
        discovery_spec_sha256=binding["discovery_spec_sha256"],
        ownership_contract_version=1,
        proposed_fields=[
            proposer.FieldProposal(field_name="capacity_tons_month", field_type="float", **binding),
            proposer.FieldProposal(field_name="tier", field_type="string", **binding),
            proposer.FieldProposal(field_name="undecided", field_type="string", **binding),
        ],
        version_bump="0.4.0-discovered",
    )
    decisions = [
        proposer.ApprovalDecision(
            node_label="Facility", field_name="capacity_tons_month", approved=True
        ),
        proposer.ApprovalDecision(node_label="Facility", field_name="tier", approved=False),
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
        ("capacity_tons_month", True),
        ("tier", False),
    ]
    assert all(all(d[key] == value for key, value in binding.items()) for d in sent["decisions"])


@requires_db_runtime
@pytest.mark.unit
def test_startup_replays_approved_rows_batch_by_batch() -> None:
    row = SimpleNamespace(
        tenant_id="plasticos",
        domain="plasticos",
        batch_run_id="run-1",
        field_name="capacity_tons_month",
        field_type="float",
        source="enrichment",
        fill_rate=1.0,
        avg_confidence=0.9,
        sample_values=[],
        value_distribution={},
        proposed_gate=None,
        proposed_scoring_dimension=None,
        approval_status="approved",
        node_label=None,
        discovery_spec_sha256=None,
        ownership_contract_version=None,
    )
    before = copy.deepcopy(vars(row))
    spec = _spec("plasticos")
    with pytest.raises(ValueError, match="^Schema proposal is legacy-unbound$"):
        _converge().apply_approved_records(spec, [row])
    assert vars(row) == before, "historical rows must never acquire inferred ownership"
    assert spec == _spec("plasticos")


def _live_version(bumps: int = 0) -> str:
    """The live plasticos version, or that version with its minor bumped `bumps` times.

    Discovery revisions are minor bumps over the live cartridge, so the expected
    versions follow whatever revision is current rather than a pinned literal.
    A zero bump is the live version unchanged, patch included. A positive bump
    is the minor increment the publisher emits, with the patch reset to 0.
    """
    spec = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "domains/plasticos/spec.yaml").read_bytes()
    )
    version = str(spec["version"])
    if bumps == 0:
        return version
    major, minor, _patch = (int(part) for part in version.split("."))
    return f"{major}.{minor + bumps}.0"


@requires_db_runtime
@pytest.mark.unit
def test_bound_startup_replay_preserves_original_discovery_and_associations() -> None:
    fixture = Path(__file__).resolve().parents[2] / "domains/plasticos/spec.yaml"
    spec = yaml.safe_load(fixture.read_bytes())
    node = spec["ontology"]["nodes"][0]
    node["properties"] = [
        prop for prop in node["properties"] if prop["name"] != "capacity_tons_month"
    ]
    node["enrichment_hints"]["priority_fields"].remove("capacity_tons_month")
    spec["inference_rules"] = [
        rule
        for rule in spec["inference_rules"]
        if all(
            member["field"] != "capacity_tons_month"
            for key in ("conditions", "outputs")
            for member in rule[key]
        )
    ]
    original_names = {prop["name"] for prop in node["properties"]}
    discovery = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True).encode("utf-8")
    digest = hashlib.sha256(discovery).hexdigest()
    row = SimpleNamespace(
        tenant_id="plasticos",
        domain="plasticos",
        batch_run_id="run-1",
        node_label="Facility",
        discovery_spec_sha256=digest,
        ownership_contract_version=1,
        field_name="capacity_tons_month",
        field_type="float",
        source="enrichment",
        fill_rate=1.0,
        avg_confidence=0.9,
        sample_values=[],
        value_distribution={},
        proposed_gate=None,
        proposed_scoring_dimension=None,
        approval_status="approved",
        reviewed_by="test-fixture-only",
        reviewed_at=datetime(2026, 11, 3, 21, 0, tzinfo=UTC),
    )
    before = copy.deepcopy(vars(row))
    revised = _converge().apply_approved_records(spec, [row], discovery_specs={digest: discovery})
    names = {p["name"] for p in revised["ontology"]["nodes"][0]["properties"]}
    assert names == original_names | {"capacity_tons_month"}
    assert revised["version"] == f"{_live_version(1)}-discovered"
    assert revised["sync"] == spec["sync"]
    assert revised["semantic_registry"] == spec["semantic_registry"]
    assert vars(row) == before


# ── 5. Readiness reflects the registry (provider runtime) ────────────────────


@requires_app_runtime
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("loaded", 200),
        ("not_attempted", 200),
        ("empty", 503),
        ("failed", 503),
        ("hydrate_failed", 503),
    ],
)
def test_readiness_is_503_while_registry_is_empty_or_failed(
    monkeypatch: pytest.MonkeyPatch, state: str, expected: int
) -> None:
    from fastapi.testclient import TestClient

    from app import main as main_module
    from app.core.config import get_settings

    assert frozenset({"empty", "failed", "hydrate_failed"}) == main_module.DOMAIN_REGISTRY_DEGRADED
    monkeypatch.setenv("GATE_REGISTRATION_ENABLED", "false")
    monkeypatch.setattr(main_module, "_domain_registry_state", state)
    get_settings.cache_clear()
    try:
        resp = TestClient(main_module.app).get(main_module.READINESS_ENDPOINT)
    finally:
        get_settings.cache_clear()
    assert resp.status_code == expected
    assert resp.json()["ready"] is (expected == 200)


# ── 6. The API contract documents the registry statuses (no runtime) ─────────


def _openapi_paths() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    spec = yaml.safe_load((root / "docs/contracts/api/openapi.yaml").read_text(encoding="utf-8"))
    return spec["paths"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method", "path", "statuses"),
    [
        ("post", "/v1/converge/{run_id}/approve", {"200", "403", "404", "409", "503"}),
        ("post", "/v1/scan", {"200", "403", "404"}),
        ("post", "/api/v1/scan", {"200", "403", "404"}),
        ("get", "/v1/converge/proposals/{domain}", {"200", "403"}),
    ],
)
def test_openapi_documents_registry_statuses(method: str, path: str, statuses: set[str]) -> None:
    responses = _openapi_paths()[path][method]["responses"]
    assert statuses <= set(responses)


@pytest.mark.unit
def test_openapi_approve_body_is_the_decisions_shape() -> None:
    """`/v1/converge/{run_id}/approve` takes decisions, not discover's single-proposal body."""
    body = _openapi_paths()["/v1/converge/{run_id}/approve"]["post"]["requestBody"]
    ref = body["content"]["application/json"]["schema"]["$ref"]
    assert ref == "#/components/schemas/ConvergeApprovalRequest"
