"""Direct-REST tenant posture witnesses (EIE-001, EIE-002, EIE-SEC-03).

L9_META:
  tier: 2
  domain: security
  authority: docs/contracts/DOMAIN_IDENTITY_REGISTRY.md section 3; EIE-SEC-03

The rule under test has one owner, ``app.core.auth.ensure_direct_tenant_read_allowed``:
the X-API-Key authenticates a caller but binds it to no tenant, so a direct,
tenant-scoped route is served only in ``DIRECT_TENANT_READ_ENVIRONMENTS`` and
answers 403 everywhere else, before any run, proposal or spec is read.

Two layers:

* behavioural witnesses for the two routes the audit found unguarded, and
* a census over the *mounted* application's OpenAPI. Every key-authenticated
  operation is either a direct tenant route (and must answer 403 in a signed
  environment, before I/O) or carries a recorded exemption. A new route that is
  neither fails the census, so the next unguarded route cannot ship silently.

Authentication itself is satisfied by overriding ``verify_api_key``: these tests
isolate the *additional* tenant posture that a shared credential cannot supply.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.v1 import converge, discover
from app.core import auth
from app.engines.convergence.loop_state import LoopState, LoopStatus
from app.services import pg_store

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
PROPOSAL = UUID("00000000-0000-0000-0000-000000000049")
RUN_ID = "cross-tenant-run-49"
SIGNED_ENVIRONMENTS = ("staging", "prod")

# Every key-authenticated operation that is NOT a direct tenant route, and why.
# Adding to this table is a reviewed decision, not a way to make the census pass.
EXEMPT_KEY_OPERATIONS: dict[tuple[str, str], str] = {
    ("post", "/api/v1/enrich"): (
        "carries no tenant: idempotency is scoped to the authenticated key "
        "(app/main.py api_key_scope, EIE-FIX-001)"
    ),
    ("post", "/api/v1/enrich/batch"): "carries no tenant: same key scope as /api/v1/enrich",
}

# Minimal well-formed inputs, so a route whose guard runs inside the handler body
# reaches that guard instead of stopping at request validation (422).
_TENANT_QUERY = {"tenant_id": "acme"}
_REQUEST_INPUTS: dict[tuple[str, str], dict[str, Any]] = {
    ("post", "/api/v1/proposals/{proposal_id}/approve"): {
        "json": {"approved": True, "reviewed_by": "Fixture Reviewer"}
    },
    ("post", "/v1/converge/{run_id}/approve"): {
        "json": {"decisions": [], "reviewed_by": "Fixture Reviewer"}
    },
    # The tenant-scoped form of each converge route names a tenant `domain`.
    # `acme` is deliberately NOT a registered spec: the posture is about which
    # tenant the caller names, not about whether this node has loaded it.
    ("post", "/v1/converge"): {"json": {"entity": {"id": "entity-49"}, "domain": "acme"}},
    ("post", "/v1/converge/batch"): {
        "json": {"domain": "acme", "inline_profile": None, "entities": [{"id": "entity-49"}]}
    },
    ("post", "/v1/scan"): {"json": {"fields": [], "domain": "acme"}},
}


def _routes() -> FastAPI:
    app = FastAPI()
    app.include_router(discover.router)
    app.include_router(converge.router)
    app.dependency_overrides[auth.verify_api_key] = lambda: "deployment-shared-key"
    return app


def _set_environment(monkeypatch: pytest.MonkeyPatch, environment: str) -> None:
    monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(l9_environment=environment))


def _foreign_run() -> LoopState:
    return LoopState(
        run_id=RUN_ID,
        entity_id="res.partner:private-49",
        domain="foreign-tenant",
        status=LoopStatus.HUMAN_HOLD,
    )


def _install_io_tripwires(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Any run-state, proposal-ledger or database access fails the test loudly."""

    def _tripped(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("tenant-scoped state was read before the posture refused")

    @asynccontextmanager
    async def _no_session() -> AsyncIterator[None]:
        _tripped()
        yield

    store = SimpleNamespace(
        load=AsyncMock(side_effect=_tripped),
        save=AsyncMock(side_effect=_tripped),
        list_active=AsyncMock(side_effect=_tripped),
    )
    monkeypatch.setattr(converge, "_state_store", store)
    monkeypatch.setattr(pg_store, "get_session", _no_session)
    return store


# ── EIE-001 / EIE-002: the two routes the audit found ─────────────────────


@pytest.mark.parametrize("environment", SIGNED_ENVIRONMENTS)
@pytest.mark.parametrize("approved", [True, False])
async def test_shared_key_cannot_decide_proposal_in_signed_environment(
    monkeypatch: pytest.MonkeyPatch, environment: str, approved: bool
) -> None:
    """EIE-001/P0: refused before the proposal UUID is even looked up."""
    _set_environment(monkeypatch, environment)
    _install_io_tripwires(monkeypatch)
    mutation = AsyncMock()
    monkeypatch.setattr(pg_store, "approve_schema_proposal", mutation)

    async with AsyncClient(transport=ASGITransport(app=_routes()), base_url="http://test") as c:
        reply = await c.post(
            f"/api/v1/proposals/{PROPOSAL}/approve",
            json={"approved": approved, "reviewed_by": "Fixture Reviewer"},
            headers={"X-API-Key": "deployment-shared-key"},
        )

    assert reply.status_code == 403, reply.text
    assert reply.json()["detail"] == auth._GATE_INGRESS_REQUIRED
    mutation.assert_not_awaited()


@pytest.mark.parametrize("environment", SIGNED_ENVIRONMENTS)
async def test_shared_key_cannot_read_run_status_in_signed_environment(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    """EIE-002/P1: knowing a run id is not authorization to read its metadata.

    The refusal must not depend on whether the run exists, or the status code
    becomes an oracle for which run ids are live.
    """
    _set_environment(monkeypatch, environment)
    store = _install_io_tripwires(monkeypatch)

    async with AsyncClient(transport=ASGITransport(app=_routes()), base_url="http://test") as c:
        known = await c.get(f"/v1/converge/{RUN_ID}", headers={"X-API-Key": "k"})
        unknown = await c.get("/v1/converge/no-such-run", headers={"X-API-Key": "k"})

    assert (known.status_code, unknown.status_code) == (403, 403), (known.text, unknown.text)
    assert known.json() == unknown.json()
    store.load.assert_not_awaited()


async def test_unsigned_environment_keeps_review_and_status_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: closing the holes must not disable the local/test API."""
    _set_environment(monkeypatch, "test")
    mutation = AsyncMock()
    load = AsyncMock(return_value=_foreign_run())
    monkeypatch.setattr(pg_store, "approve_schema_proposal", mutation)
    monkeypatch.setattr(converge, "_state_store", SimpleNamespace(load=load))

    async with AsyncClient(transport=ASGITransport(app=_routes()), base_url="http://test") as c:
        review = await c.post(
            f"/api/v1/proposals/{PROPOSAL}/approve",
            json={"approved": True, "reviewed_by": "Fixture Reviewer"},
            headers={"X-API-Key": "deployment-shared-key"},
        )
        status = await c.get(f"/v1/converge/{RUN_ID}", headers={"X-API-Key": "k"})

    assert review.status_code == 200, review.text
    assert review.json() == {"status": "approved", "proposal_id": str(PROPOSAL)}
    mutation.assert_awaited_once_with(
        proposal_id=PROPOSAL, reviewed_by="Fixture Reviewer", approved=True
    )
    assert status.status_code == 200, status.text
    assert status.json()["run_id"] == RUN_ID
    load.assert_awaited_once_with(RUN_ID)


@pytest.mark.parametrize("environment", SIGNED_ENVIRONMENTS)
async def test_batch_converge_naming_an_unloaded_tenant_is_refused_before_any_run(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    """EIE-008/P1: the batch guard must not depend on this node having loaded the spec.

    `POST /v1/converge` refuses any request that names a `domain`. The batch
    route only refused when the domain was already registered (or a node label
    was given), so a shared-key caller could name any other tenant id and have
    paid convergence runs persisted under that tenant's domain.
    """
    _set_environment(monkeypatch, environment)
    store = _install_io_tripwires(monkeypatch)
    profile = SimpleNamespace(profile_name="fixture", batch_size=5, max_passes=1)
    monkeypatch.setattr(converge, "_profile_registry", SimpleNamespace(get=lambda _n: profile))
    monkeypatch.setattr(converge, "_domain_specs", {})
    loop = AsyncMock(side_effect=AssertionError("a convergence run started"))
    monkeypatch.setattr(
        "app.engines.convergence_controller.run_convergence_loop", loop, raising=True
    )

    async with AsyncClient(transport=ASGITransport(app=_routes()), base_url="http://test") as c:
        reply = await c.post(
            "/v1/converge/batch",
            json={"profile_name": "fixture", "domain": "acme", "entities": [{"id": "e-49"}]},
            headers={"X-API-Key": "k"},
        )

    assert reply.status_code == 403, reply.text
    store.save.assert_not_awaited()
    loop.assert_not_awaited()


@pytest.mark.parametrize("environment", SIGNED_ENVIRONMENTS)
async def test_domainless_batch_converge_is_refused_before_any_run(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    """A batch answers only run ids, and GET /v1/converge/{run_id} is refused here.

    So a signed environment must not start batch runs at all: the caller would
    pay for convergence whose outcome no route can return to it.
    """
    _set_environment(monkeypatch, environment)
    store = _install_io_tripwires(monkeypatch)
    profile = SimpleNamespace(profile_name="fixture", batch_size=5, max_passes=1)
    monkeypatch.setattr(converge, "_profile_registry", SimpleNamespace(get=lambda _n: profile))
    loop = AsyncMock(side_effect=AssertionError("a convergence run started"))
    monkeypatch.setattr(
        "app.engines.convergence_controller.run_convergence_loop", loop, raising=True
    )

    async with AsyncClient(transport=ASGITransport(app=_routes()), base_url="http://test") as c:
        reply = await c.post(
            "/v1/converge/batch",
            json={"profile_name": "fixture", "entities": [{"id": "e-49"}]},
            headers={"X-API-Key": "k"},
        )

    assert reply.status_code == 403, reply.text
    store.save.assert_not_awaited()
    loop.assert_not_awaited()


# ── The posture itself fails closed ───────────────────────────────────────


@pytest.mark.parametrize(
    "environment",
    ["staging", "prod", "production", "Local", "dev ", "", "unknown"],
)
def test_posture_refuses_every_environment_outside_the_allowlist(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    """A misspelled or unexpected environment is signed, never trusted."""
    from fastapi import HTTPException

    _set_environment(monkeypatch, environment)
    with pytest.raises(HTTPException) as caught:
        auth.ensure_direct_tenant_read_allowed()
    assert caught.value.status_code == 403


# ── Census over the mounted application ───────────────────────────────────


def _key_authenticated_operations() -> list[tuple[str, str]]:
    """(method, path) of every operation the real app documents as X-API-Key secured."""
    from app.main import app as node_app

    schema = node_app.openapi()
    schemes = {
        name
        for name, spec in schema.get("components", {}).get("securitySchemes", {}).items()
        if spec.get("type") == "apiKey" and spec.get("name") == "X-API-Key"
    }
    found = []
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            requirements = operation.get("security", [])
            if any(name in schemes for requirement in requirements for name in requirement):
                found.append((method, path))
    return sorted(found)


_DIRECT_TENANT_OPERATIONS = [
    op for op in _key_authenticated_operations() if op not in EXEMPT_KEY_OPERATIONS
]


def test_census_finds_the_known_direct_tenant_operations() -> None:
    """The census is not vacuous: it sees the routes the audit is about."""
    assert ("post", "/api/v1/proposals/{proposal_id}/approve") in _DIRECT_TENANT_OPERATIONS
    assert ("get", "/v1/converge/{run_id}") in _DIRECT_TENANT_OPERATIONS
    assert set(EXEMPT_KEY_OPERATIONS) <= set(_key_authenticated_operations()), (
        "an exemption names an operation the app no longer serves; remove it"
    )


def _fill_path(path: str) -> str:
    return (
        path.replace("{proposal_id}", str(PROPOSAL))
        .replace("{run_id}", RUN_ID)
        .replace("{domain}", "acme")
        .replace("{entity_id}", "entity-49")
        .replace("{field_name}", "field-49")
        .replace("{profile_id}", "profile-49")
    )


@pytest.mark.parametrize("environment", SIGNED_ENVIRONMENTS)
@pytest.mark.parametrize(
    ("method", "path"),
    _DIRECT_TENANT_OPERATIONS,
    ids=[f"{m} {p}" for m, p in _DIRECT_TENANT_OPERATIONS],
)
async def test_every_direct_tenant_operation_refuses_shared_key_in_signed_environment(
    monkeypatch: pytest.MonkeyPatch, environment: str, method: str, path: str
) -> None:
    """Each non-exempt key-authenticated operation answers 403 before tenant state I/O."""
    from app.main import app as node_app

    _set_environment(monkeypatch, environment)
    _install_io_tripwires(monkeypatch)
    monkeypatch.setitem(node_app.dependency_overrides, auth.verify_api_key, lambda: "k")
    inputs = _REQUEST_INPUTS.get((method, path), {})
    if method in {"post", "put", "patch"}:
        inputs = {"json": {}, **inputs}

    async with AsyncClient(transport=ASGITransport(app=node_app), base_url="http://test") as c:
        reply = await c.request(
            method.upper(),
            _fill_path(path),
            params=_TENANT_QUERY,
            headers={"X-API-Key": "k"},
            **inputs,
        )

    assert reply.status_code == 403, f"{method.upper()} {path}: {reply.status_code} {reply.text}"
