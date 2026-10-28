"""
HubSpot Record Read Contract
Source: app/services/crm/hubspot_client.py — HubSpotClient.get_record.
Markers: unit

CRMClientBase.get_record returns the record's ``properties`` dict, ``None`` for
an unknown record, and ``None`` (logged) for any other failure. Writeback
callers branch on exactly that shape, so the typed return in
hubspot_client.py is a contract with them, not an implementation detail.

Every app import is deferred into a test body. The L9 Constitution Gate
collects tests/contracts/ in a lean environment without the provider SDKs, and
a module-level app import here breaks collection (see
test_soql_literal_contract.py, which documents the same constraint).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _client() -> Any:
    from app.services.crm.base import CRMCredentials, CRMType
    from app.services.crm.hubspot_client import HubSpotClient

    return HubSpotClient(
        CRMCredentials(crm_type=CRMType.HUBSPOT, credentials={"access_token": "t"})
    )


def _response(status: int, body: Any = None) -> MagicMock:
    import httpx

    response = MagicMock(spec=httpx.Response)
    response.status_code = status
    response.headers = {}
    response.json.return_value = body
    if status >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=response
        )
    return response


def _patched_get(get: MagicMock) -> Any:
    from app.services.crm import hubspot_client as hubspot_module

    return patch.object(hubspot_module.httpx, "get", get)


def test_get_record_returns_the_properties_dict() -> None:
    body = {"id": "101", "properties": {"name": "Synthetic Co", "domain": "example.invalid"}}
    get = MagicMock(return_value=_response(200, body))
    with _patched_get(get):
        record = _client().get_record("company", "101")
    assert record == {"name": "Synthetic Co", "domain": "example.invalid"}
    assert get.call_args.args[0] == "https://api.hubapi.com/crm/v3/objects/companies/101"


def test_get_record_without_properties_returns_an_empty_dict() -> None:
    with _patched_get(MagicMock(return_value=_response(200, {"id": "101"}))):
        assert _client().get_record("company", "101") == {}


@pytest.mark.parametrize("status", [404, 403, 500])
def test_get_record_returns_none_for_unknown_or_failed_reads(status: int) -> None:
    with _patched_get(MagicMock(return_value=_response(status, {}))):
        assert _client().get_record("company", "101") is None
