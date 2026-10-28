"""
tests/unit/test_hubspot_get_record.py

HubSpotClient.get_record returns the record's ``properties`` dict, ``None`` for
an unknown record, and ``None`` (logged) for any other failure. Pins the
behaviour the typed return in ``hubspot_client.py`` keeps.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.services.crm import hubspot_client as hubspot_module
from app.services.crm.base import CRMCredentials, CRMType
from app.services.crm.hubspot_client import HubSpotClient

pytestmark = pytest.mark.unit


def _client() -> HubSpotClient:
    return HubSpotClient(
        CRMCredentials(crm_type=CRMType.HUBSPOT, credentials={"access_token": "t"})
    )


def _response(status: int, body: Any = None) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status
    response.headers = {}
    response.json.return_value = body
    if status >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=response
        )
    return response


def test_get_record_returns_the_properties_dict() -> None:
    body = {"id": "101", "properties": {"name": "Synthetic Co", "domain": "example.invalid"}}
    get = MagicMock(return_value=_response(200, body))
    with patch.object(hubspot_module.httpx, "get", get):
        record = _client().get_record("company", "101")
    assert record == {"name": "Synthetic Co", "domain": "example.invalid"}
    assert get.call_args.args[0] == "https://api.hubapi.com/crm/v3/objects/companies/101"


def test_get_record_without_properties_returns_an_empty_dict() -> None:
    with patch.object(
        hubspot_module.httpx, "get", MagicMock(return_value=_response(200, {"id": "101"}))
    ):
        assert _client().get_record("company", "101") == {}


@pytest.mark.parametrize("status", [404, 403, 500])
def test_get_record_returns_none_for_unknown_or_failed_reads(status: int) -> None:
    with patch.object(hubspot_module.httpx, "get", MagicMock(return_value=_response(status, {}))):
        assert _client().get_record("company", "101") is None
