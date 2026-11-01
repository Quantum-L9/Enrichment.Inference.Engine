"""
HubSpot Record Read Contract
Source: app/services/crm/hubspot_client.py — HubSpotClient.get_record.
Markers: unit

CRMClientBase.get_record returns the normalized properties dict, including the
canonical record id, ``None`` only for a documented HTTP 404, and
``CRMReadError`` for every other read failure. Writeback callers branch on
that shape.

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


def test_normalize_record_keeps_the_provider_account() -> None:
    from app.services.crm.base import CRMCredentials, CRMType
    from app.services.crm.hubspot_client import HubSpotClient

    client = HubSpotClient(
        CRMCredentials(
            crm_type=CRMType.HUBSPOT,
            credentials={"access_token": "t"},
            provider_account_id="portal-a",
        )
    )
    record = client.normalize_record(
        "tenant-a", "hubspot-a", "company", {"id": "101", "properties": {}}
    )
    assert record.record_ref.provider_account_id == "portal-a"


def test_get_record_returns_the_properties_dict() -> None:
    body = {"id": "101", "properties": {"name": "Synthetic Co", "domain": "example.invalid"}}
    get = MagicMock(return_value=_response(200, body))
    with _patched_get(get):
        record = _client().get_record("company", "101")
    assert record == {"id": "101", "name": "Synthetic Co", "domain": "example.invalid"}
    assert get.call_args.args[0] == "https://api.hubapi.com/crm/v3/objects/companies/101"


def test_get_record_without_properties_returns_the_canonical_id() -> None:
    with _patched_get(MagicMock(return_value=_response(200, {"id": "101"}))):
        assert _client().get_record("company", "101") == {"id": "101"}


def test_get_record_returns_none_for_unknown_record() -> None:
    with _patched_get(MagicMock(return_value=_response(404, {}))):
        assert _client().get_record("company", "101") is None


@pytest.mark.parametrize("status", [403, 500])
def test_get_record_raises_for_failed_reads(status: int) -> None:
    from app.services.crm.base import CRMReadError

    with (
        _patched_get(MagicMock(return_value=_response(status, {}))),
        pytest.raises(CRMReadError),
    ):
        _client().get_record("company", "101")
