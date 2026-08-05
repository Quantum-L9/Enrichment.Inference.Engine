"""Canonical converge contract fixtures (TASK-005, EIE-owned half).

The converge action (over /v1/execute) uses EnrichRequest for the request and
EnrichResponse for the response. These tests pin the canonical fixtures against
the live Pydantic models so contract drift is caught at test time.

Cost is expressed as ``tokens_used`` (int); EnrichResponse has NO
``total_cost_usd`` — this is asserted explicitly to prevent reintroduction.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.schemas import EnrichRequest, EnrichResponse

_CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"
_REQUEST_FIXTURE = _CONTRACTS_DIR / "converge_request.json"
_RESPONSE_FIXTURE = _CONTRACTS_DIR / "converge_response.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_request_fixture_validates():
    payload = _load(_REQUEST_FIXTURE)
    model = EnrichRequest.model_validate(payload)
    assert model.entity
    assert model.object_type
    assert model.objective


def test_response_fixture_validates():
    payload = _load(_RESPONSE_FIXTURE)
    model = EnrichResponse.model_validate(payload)
    assert model.state == "completed"
    assert isinstance(model.tokens_used, int)


def test_response_has_no_total_cost_usd():
    payload = _load(_RESPONSE_FIXTURE)
    assert "total_cost_usd" not in payload
    # The model must not expose it either.
    assert "total_cost_usd" not in EnrichResponse.model_fields


def test_dropping_required_request_field_fails_validation():
    payload = _load(_REQUEST_FIXTURE)
    payload.pop("objective")
    with pytest.raises(ValidationError):
        EnrichRequest.model_validate(payload)
