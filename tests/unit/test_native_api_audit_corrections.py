"""PR-281-03 API error and explicit-reviewer regressions, without a live database.

L9_META:
  tier: 2
  authority: native-hosted-correction-g27r-20261104
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api.v1 import converge, discover
from app.core.auth import verify_api_key
from app.services import pg_store

pytestmark = pytest.mark.unit
_PROPOSAL_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_APPROVAL_ROUTES = (
    ("/v1/converge/missing-run/approve", {"decisions": []}, converge.ApproveRequestBody),
    (
        f"/api/v1/proposals/{_PROPOSAL_ID}/approve",
        {"approved": True},
        discover.ApprovalRequest,
    ),
)


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Supply a SELECT result and write spies, not a PostgreSQL transaction proof."""
    session = SimpleNamespace(row=None, flush=AsyncMock(), add=Mock())
    session.execute = AsyncMock(
        side_effect=lambda _statement: SimpleNamespace(scalar_one_or_none=lambda: session.row)
    )

    @asynccontextmanager
    async def context() -> AsyncIterator[SimpleNamespace]:
        yield session

    monkeypatch.setattr(pg_store, "get_session", context)
    return session


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Mount real routers without startup services; override authentication only in this app."""
    app = FastAPI()
    app.include_router(converge.router)
    app.include_router(discover.router)
    app.dependency_overrides[verify_api_key] = lambda: None
    state_store = SimpleNamespace(load=AsyncMock(return_value=None), save=AsyncMock())
    ledger = AsyncMock()
    review_log = Mock()
    registry: dict[str, Any] = {}
    monkeypatch.setattr(converge, "require_direct_domain_access", lambda: None)
    monkeypatch.setattr(converge, "_state_store", state_store)
    monkeypatch.setattr(converge, "_domain_specs", registry)
    monkeypatch.setattr(pg_store, "persist_schema_proposal_batch", ledger)
    monkeypatch.setattr(discover, "logger", SimpleNamespace(info=review_log))
    return SimpleNamespace(
        app=app, store=state_store, ledger=ledger, registry=registry, review_log=review_log
    )


async def _post(api: SimpleNamespace, path: str, body: dict[str, Any]) -> Any:
    async with AsyncClient(transport=ASGITransport(app=api.app), base_url="http://test") as client:
        return await client.post(path, json=body)


def _record(**changes: Any) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "id": _PROPOSAL_ID,
            "tenant_id": "acme",
            "domain": "acme",
            "batch_run_id": "discovery-1",
            "node_label": "Buyer",
            "discovery_spec_sha256": "a" * 64,
            "ownership_contract_version": 1,
            "approval_status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
            **changes,
        }
    )


@pytest.mark.parametrize("approved", [True, False])
async def test_missing_uuid_is_lookup_error_without_update(session, approved):
    with pytest.raises(LookupError):
        await pg_store.approve_schema_proposal(_PROPOSAL_ID, "test-fixture-only", approved)
    session.execute.assert_awaited_once()
    assert session.execute.await_args.args[0].is_select
    session.flush.assert_not_awaited()
    session.add.assert_not_called()


@pytest.mark.parametrize("approved", [True, False])
async def test_missing_uuid_route_is_404_without_review_side_effect(api, session, approved):
    response = await _post(
        api,
        f"/api/v1/proposals/{_PROPOSAL_ID}/approve",
        {"approved": approved, "reviewed_by": "test-fixture-only"},
    )
    assert response.status_code == 404, response.text
    session.flush.assert_not_awaited()
    session.add.assert_not_called()
    api.review_log.assert_not_called()
    api.ledger.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [(LookupError("An absent UUID"), 404), (ValueError("Schema proposal not found"), 409)],
)
async def test_route_dispatches_exception_types_not_message_text(
    api, monkeypatch, error, expected_status
):
    approve = AsyncMock(side_effect=error)
    monkeypatch.setattr(pg_store, "approve_schema_proposal", approve)
    response = await _post(
        api,
        f"/api/v1/proposals/{_PROPOSAL_ID}/approve",
        {"approved": True, "reviewed_by": "test-fixture-only"},
    )
    assert response.status_code == expected_status, response.text
    approve.assert_awaited_once()
    api.review_log.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"node_label": None, "discovery_spec_sha256": None, "ownership_contract_version": None},
        {"discovery_spec_sha256": "wrong-binding"},
        {"domain": "other"},
        {"approval_status": "rejected", "reviewed_by": "original-reviewer"},
    ],
    ids=["historical-unbound", "malformed-binding", "wrong-scope", "conflicting-review"],
)
async def test_binding_and_review_conflicts_remain_409_without_mutation(api, session, changes):
    session.row = _record(**changes)
    before = copy.deepcopy(vars(session.row))
    response = await _post(
        api,
        f"/api/v1/proposals/{_PROPOSAL_ID}/approve",
        {"approved": True, "reviewed_by": "test-fixture-only"},
    )
    assert response.status_code == 409, response.text
    assert vars(session.row) == before
    session.flush.assert_not_awaited()
    session.add.assert_not_called()
    api.review_log.assert_not_called()
    api.ledger.assert_not_awaited()


@pytest.mark.parametrize(("path", "body", "model"), _APPROVAL_ROUTES)
def test_reviewer_omission_is_rejected_by_the_dto(path, body, model):
    with pytest.raises(ValidationError) as caught:
        model.model_validate(body)
    assert any(error["loc"] == ("reviewed_by",) for error in caught.value.errors())


@pytest.mark.parametrize(("path", "body", "model"), _APPROVAL_ROUTES)
def test_generated_openapi_requires_reviewer_with_no_default(api, path, body, model):
    schema = api.app.openapi()
    route = (
        "/v1/converge/{run_id}/approve"
        if path.startswith("/v1/")
        else "/api/v1/proposals/{proposal_id}/approve"
    )
    request = schema["paths"][route]["post"]["requestBody"]
    assert request["required"] is True
    ref = request["content"]["application/json"]["schema"]["$ref"]
    dto = schema["components"]["schemas"][ref.rsplit("/", 1)[1]]
    assert "reviewed_by" in dto["required"]
    assert "default" not in dto["properties"]["reviewed_by"]


@pytest.mark.parametrize(("path", "body", "model"), _APPROVAL_ROUTES)
@pytest.mark.parametrize("reviewer", [None, "", " ", "\t\n", " reviewer", "reviewer ", "r" * 257])
async def test_invalid_reviewer_is_422_before_state_or_audit(
    api, session, path, body, model, reviewer
):
    response = await _post(api, path, {**body, "reviewed_by": reviewer})
    assert response.status_code == 422, response.text
    assert any(error["loc"] == ["body", "reviewed_by"] for error in response.json()["detail"])
    api.store.load.assert_not_awaited()
    api.store.save.assert_not_awaited()
    api.ledger.assert_not_awaited()
    api.review_log.assert_not_called()
    session.execute.assert_not_awaited()
    session.flush.assert_not_awaited()
    assert api.registry == {}


@pytest.mark.parametrize(("path", "body", "model"), _APPROVAL_ROUTES)
async def test_omitted_reviewer_is_422_without_audit_side_effect(api, session, path, body, model):
    response = await _post(api, path, body)
    assert response.status_code == 422, response.text
    assert any(error["loc"] == ["body", "reviewed_by"] for error in response.json()["detail"])
    api.store.load.assert_not_awaited()
    api.store.save.assert_not_awaited()
    api.ledger.assert_not_awaited()
    api.review_log.assert_not_called()
    session.execute.assert_not_awaited()
    session.flush.assert_not_awaited()
    assert api.registry == {}


async def test_named_reviewer_with_missing_run_is_404_without_audit(api, session):
    response = await _post(
        api,
        "/v1/converge/missing-run/approve",
        {"decisions": [], "reviewed_by": "test-fixture-only"},
    )
    assert response.status_code == 404, response.text
    api.store.load.assert_awaited_once_with("missing-run")
    api.store.save.assert_not_awaited()
    api.ledger.assert_not_awaited()
    session.execute.assert_not_awaited()


async def test_explicit_named_reviewer_reaches_bound_review_without_changing_provenance(
    api, session
):
    session.row = _record()
    binding = (
        session.row.node_label,
        session.row.discovery_spec_sha256,
        session.row.ownership_contract_version,
    )
    response = await _post(
        api,
        f"/api/v1/proposals/{_PROPOSAL_ID}/approve",
        {"approved": True, "reviewed_by": "Fixture Reviewer"},
    )
    assert response.status_code == 200, response.text
    assert session.row.reviewed_by == "Fixture Reviewer"
    assert session.row.reviewed_at is not None
    assert session.row.approval_status == "approved"
    assert (
        session.row.node_label,
        session.row.discovery_spec_sha256,
        session.row.ownership_contract_version,
    ) == binding
    session.flush.assert_awaited_once()
    api.review_log.assert_called_once()
