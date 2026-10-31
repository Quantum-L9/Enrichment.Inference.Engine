"""Deterministic parity checks for the three CRM adapter safety contracts."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.services.crm.base import (
    CRMCredentials,
    CRMErrorCode,
    CRMReadError,
    CRMRecordRef,
    CRMType,
    CRMWritePermit,
    CRMWriteState,
    _issue_write_permit,
)
from app.services.crm.hubspot_client import HubSpotClient
from app.services.crm.odoo_client import OdooClient
from app.services.crm.salesforce_client import SalesforceClient


@pytest.fixture
def odoo_client() -> OdooClient:
    client = OdooClient(
        CRMCredentials(
            CRMType.ODOO,
            {"url": "https://odoo.test", "db": "test", "username": "user", "password": "secret"},
        )
    )
    client._uid = 1
    return client


@pytest.fixture
def hubspot_client() -> HubSpotClient:
    return HubSpotClient(CRMCredentials(CRMType.HUBSPOT, {"access_token": "test-token"}))


@pytest.fixture
def salesforce_client() -> SalesforceClient:
    client = SalesforceClient(CRMCredentials(CRMType.SALESFORCE, {}))
    client._instance_url = "https://salesforce.test"
    client._access_token = "test-token"
    return client


def _permit(
    crm_type: CRMType,
    object_type: str,
    record_id: str,
    data: dict[str, Any],
) -> CRMWritePermit:
    return _issue_write_permit(
        "operation-1",
        CRMRecordRef(
            tenant_id="tenant-a",
            connector_id="connector-a",
            crm_type=crm_type,
            object_type=object_type,
            record_id=record_id,
        ),
        "a" * 64,
        data,
    )


def test_odoo_query_timeout_is_not_interpreted_as_not_found(odoo_client: OdooClient) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("network timeout")

    odoo_client._execute = fail  # type: ignore[method-assign]

    with pytest.raises(CRMReadError) as error:
        odoo_client.query_records("res.partner", {"name": "Acme"})

    assert error.value.code == CRMErrorCode.TIMEOUT


def test_hubspot_empty_complete_query_is_not_found_compatible(
    monkeypatch: pytest.MonkeyPatch, hubspot_client: HubSpotClient
) -> None:
    monkeypatch.setattr(
        "app.services.crm.hubspot_client.httpx.post",
        lambda *args, **kwargs: httpx.Response(200, json={"results": []}),
    )

    assert hubspot_client.query_records("companies", {"domain": "acme.test"}) == []


def test_hubspot_query_preserves_documented_top_level_id(
    monkeypatch: pytest.MonkeyPatch, hubspot_client: HubSpotClient
) -> None:
    monkeypatch.setattr(
        "app.services.crm.hubspot_client.httpx.post",
        lambda *args, **kwargs: httpx.Response(
            200,
            json={"results": [{"id": "42", "properties": {"name": "Acme"}}]},
        ),
    )

    records = hubspot_client.query_records("companies", {"name": "Acme"})

    assert records == [{"name": "Acme", "id": "42"}]


def test_hubspot_query_rate_limit_is_an_error_not_empty_result(
    monkeypatch: pytest.MonkeyPatch, hubspot_client: HubSpotClient
) -> None:
    monkeypatch.setattr(
        "app.services.crm.hubspot_client.httpx.post",
        lambda *args, **kwargs: httpx.Response(429, json={"message": "limited"}),
    )

    with pytest.raises(CRMReadError) as error:
        hubspot_client.query_records("companies", {"domain": "acme.test"})

    assert error.value.code == CRMErrorCode.RATE_LIMIT


def test_hubspot_connection_rejects_mismatched_trusted_portal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HubSpotClient(
        CRMCredentials(CRMType.HUBSPOT, {"access_token": "test-token"}, "portal-a")
    )
    monkeypatch.setattr(
        "app.services.crm.hubspot_client.httpx.get",
        lambda *args, **kwargs: httpx.Response(200, json={"portalId": "portal-b"}),
    )

    assert client.connect() is False


def test_salesforce_empty_complete_query_is_not_found_compatible(
    monkeypatch: pytest.MonkeyPatch, salesforce_client: SalesforceClient
) -> None:
    monkeypatch.setattr(
        "app.services.crm.salesforce_client.httpx.get",
        lambda *args, **kwargs: httpx.Response(200, json={"done": True, "records": []}),
    )

    assert salesforce_client.query_records("Account", {"Name": "Acme"}) == []


def test_salesforce_incomplete_query_never_establishes_absence(
    monkeypatch: pytest.MonkeyPatch, salesforce_client: SalesforceClient
) -> None:
    monkeypatch.setattr(
        "app.services.crm.salesforce_client.httpx.get",
        lambda *args, **kwargs: httpx.Response(200, json={"done": False, "records": []}),
    )

    with pytest.raises(CRMReadError) as error:
        salesforce_client.query_records("Account", {"Name": "Acme"})

    assert error.value.code == CRMErrorCode.PAGINATION_INCOMPLETE


def test_odoo_false_write_acknowledgement_is_rejected(odoo_client: OdooClient) -> None:
    odoo_client._execute = lambda *args, **kwargs: False  # type: ignore[method-assign]

    result = odoo_client.update_record(
        "res.partner",
        "42",
        {"industry": "recycling"},
        permit=_permit(CRMType.ODOO, "res.partner", "42", {"industry": "recycling"}),
    )

    assert result.success is False
    assert result.status == CRMWriteState.REJECTED
    assert result.fields_written == []


def test_caller_constructed_permit_cannot_bypass_adapter_guard(odoo_client: OdooClient) -> None:
    odoo_client._execute = lambda *args, **kwargs: pytest.fail("provider call must not occur")  # type: ignore[method-assign]
    forged_permit = CRMWritePermit(
        operation_id="forged",
        record_ref=CRMRecordRef(
            tenant_id="tenant-a",
            connector_id="connector-a",
            crm_type=CRMType.ODOO,
            object_type="res.partner",
            record_id="42",
        ),
        payload_hash="a" * 64,
        payload_digest="forged",
    )

    result = odoo_client.update_record(
        "res.partner",
        "42",
        {"industry": "recycling"},
        permit=forged_permit,
    )

    assert result.status == CRMWriteState.DISABLED
    assert result.error == CRMErrorCode.WRITE_PERMIT_REQUIRED.value


def test_issued_permit_cannot_be_reused_for_a_different_payload(odoo_client: OdooClient) -> None:
    odoo_client._execute = lambda *args, **kwargs: pytest.fail("provider call must not occur")  # type: ignore[method-assign]

    result = odoo_client.update_record(
        "res.partner",
        "42",
        {"industry": "different"},
        permit=_permit(CRMType.ODOO, "res.partner", "42", {"industry": "recycling"}),
    )

    assert result.status == CRMWriteState.REJECTED
    assert result.error == CRMErrorCode.CONFLICT.value


def test_odoo_true_write_acknowledgement_is_confirmed(odoo_client: OdooClient) -> None:
    odoo_client._execute = lambda *args, **kwargs: True  # type: ignore[method-assign]

    result = odoo_client.update_record(
        "res.partner",
        "42",
        {"industry": "recycling"},
        permit=_permit(CRMType.ODOO, "res.partner", "42", {"industry": "recycling"}),
    )

    assert result.success is True
    assert result.status == CRMWriteState.CONFIRMED
    assert result.provider_ack == "odoo.write:true"


def test_hubspot_valid_update_acknowledgement_is_confirmed(
    monkeypatch: pytest.MonkeyPatch, hubspot_client: HubSpotClient
) -> None:
    monkeypatch.setattr(
        "app.services.crm.hubspot_client.httpx.patch",
        lambda *args, **kwargs: httpx.Response(200, json={"id": "42", "properties": {}}),
    )

    result = hubspot_client.update_record(
        "companies",
        "42",
        {"industry": "recycling"},
        permit=_permit(CRMType.HUBSPOT, "companies", "42", {"industry": "recycling"}),
    )

    assert result.success is True
    assert result.status == CRMWriteState.CONFIRMED
    assert result.reconciliation_state == "not_verified"


def test_hubspot_mismatched_success_acknowledgement_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch, hubspot_client: HubSpotClient
) -> None:
    monkeypatch.setattr(
        "app.services.crm.hubspot_client.httpx.patch",
        lambda *args, **kwargs: httpx.Response(200, json={"id": "43", "properties": {}}),
    )

    result = hubspot_client.update_record(
        "companies",
        "42",
        {"industry": "recycling"},
        permit=_permit(CRMType.HUBSPOT, "companies", "42", {"industry": "recycling"}),
    )

    assert result.status == CRMWriteState.INDETERMINATE
    assert result.reconciliation_state == "required"


def test_hubspot_timeout_after_update_attempt_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch, hubspot_client: HubSpotClient
) -> None:
    def timeout(*args: Any, **kwargs: Any) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr("app.services.crm.hubspot_client.httpx.patch", timeout)

    result = hubspot_client.update_record(
        "companies",
        "42",
        {"industry": "recycling"},
        permit=_permit(CRMType.HUBSPOT, "companies", "42", {"industry": "recycling"}),
    )

    assert result.success is False
    assert result.status == CRMWriteState.INDETERMINATE
    assert result.reconciliation_state == "required"


def test_salesforce_204_patch_is_confirmed(
    monkeypatch: pytest.MonkeyPatch, salesforce_client: SalesforceClient
) -> None:
    record_id = "001ABCDEF012345"
    monkeypatch.setattr(
        "app.services.crm.salesforce_client.httpx.patch",
        lambda *args, **kwargs: httpx.Response(204),
    )

    result = salesforce_client.update_record(
        "Account",
        record_id,
        {"Industry": "Recycling"},
        permit=_permit(CRMType.SALESFORCE, "Account", record_id, {"Industry": "Recycling"}),
    )

    assert result.success is True
    assert result.status == CRMWriteState.CONFIRMED
    assert result.provider_ack == "http:204"


@pytest.mark.parametrize("client_name", ["odoo", "hubspot", "salesforce"])
def test_direct_create_upsert_and_bulk_operations_are_disabled(
    client_name: str,
    odoo_client: OdooClient,
    hubspot_client: HubSpotClient,
    salesforce_client: SalesforceClient,
) -> None:
    client = {"odoo": odoo_client, "hubspot": hubspot_client, "salesforce": salesforce_client}[
        client_name
    ]

    assert client.create_record("companies", {"name": "Acme"}).status == CRMWriteState.DISABLED
    assert client.upsert_record("companies", "external_id", "id-1", {"name": "Acme"}).status == (
        CRMWriteState.DISABLED
    )
    assert all(
        result.status == CRMWriteState.DISABLED
        for result in client.bulk_update("companies", [{"id": "42", "name": "Acme"}])
    )
