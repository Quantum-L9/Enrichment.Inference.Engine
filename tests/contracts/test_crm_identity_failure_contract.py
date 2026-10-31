"""Shared CRM identity and failure-semantics contract tests."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.crm.base import (
    CRMClientBase,
    CRMCredentials,
    CRMErrorCode,
    CRMLookupStatus,
    CRMReadError,
    CRMRecord,
    CRMRecordRef,
    CRMType,
    CRMWritePermit,
    CRMWriteState,
    WriteResult,
)


class FakeCRMClient(CRMClientBase):
    """Deterministic fourth-provider stand-in proving the base contract is vendor-neutral."""

    def __init__(
        self, rows: list[dict[str, Any]] | None = None, error: CRMReadError | None = None
    ) -> None:
        super().__init__(CRMCredentials(CRMType.HUBSPOT, {}))
        self.rows = rows or []
        self.error = error

    def connect(self) -> bool:
        return True

    def test_connection(self) -> bool:
        return True

    def get_record(self, object_type: str, record_id: str) -> dict[str, Any] | None:
        return None

    def query_records(
        self,
        object_type: str,
        filters: dict[str, Any],
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if self.error is not None:
            raise self.error
        return [dict(row) for row in self.rows]

    def normalize_record(
        self,
        tenant_id: str,
        connector_id: str,
        object_type: str,
        record: dict[str, Any],
    ) -> CRMRecord:
        record_id = self.normalize_record_id(record.get("id"))
        return CRMRecord(
            record_ref=CRMRecordRef(
                tenant_id=tenant_id,
                connector_id=connector_id,
                crm_type=self.credentials.crm_type,
                object_type=object_type,
                record_id=record_id,
            ),
            properties=dict(record),
        )

    def create_record(self, object_type: str, data: dict[str, Any]) -> WriteResult:
        return self._unsafe_operation_result("create")

    def update_record(
        self,
        object_type: str,
        record_id: str,
        data: dict[str, Any],
        *,
        permit: CRMWritePermit | None = None,
    ) -> WriteResult:
        denied = self.validate_update_permit(object_type, record_id, data, permit)
        return denied or WriteResult(success=True, record_id=record_id, fields_written=list(data))

    def upsert_record(
        self,
        object_type: str,
        external_id_field: str,
        external_id_value: str,
        data: dict[str, Any],
    ) -> WriteResult:
        return self._unsafe_operation_result("upsert")

    def get_field_metadata(self, object_type: str) -> dict[str, Any]:
        return {}


@pytest.mark.parametrize(
    ("rows", "status", "record_id", "ambiguous_count"),
    [
        ([], CRMLookupStatus.NOT_FOUND, None, 0),
        ([{"id": 42, "name": "Acme"}], CRMLookupStatus.FOUND, "42", 0),
        ([{"id": "42"}, {"id": "43"}], CRMLookupStatus.AMBIGUOUS, None, 2),
    ],
)
def test_lookup_unique_has_explicit_zero_one_many_semantics(
    rows: list[dict[str, Any]],
    status: CRMLookupStatus,
    record_id: str | None,
    ambiguous_count: int,
) -> None:
    client = FakeCRMClient(rows=rows)

    result = client.lookup_unique("tenant-a", "connector-a", "companies", {"domain": "acme.test"})

    assert result.status == status
    assert result.ambiguous_count == ambiguous_count
    assert (result.record.record_ref.record_id if result.record else None) == record_id


def test_lookup_unique_rejects_found_record_without_an_identifier() -> None:
    client = FakeCRMClient(rows=[{"name": "Acme"}])

    with pytest.raises(CRMReadError) as error:
        client.lookup_unique("tenant-a", "connector-a", "companies", {"name": "Acme"})

    assert error.value.code == CRMErrorCode.MALFORMED_RESPONSE


def test_lookup_unique_propagates_remote_failure_instead_of_returning_empty_list() -> None:
    client = FakeCRMClient(
        error=CRMReadError(CRMErrorCode.AUTHORIZATION, "provider denied read", retriable=False)
    )

    with pytest.raises(CRMReadError) as error:
        client.lookup_unique("tenant-a", "connector-a", "companies", {"name": "Acme"})

    assert error.value.code == CRMErrorCode.AUTHORIZATION


def test_indeterminate_result_never_projects_to_legacy_success() -> None:
    result = WriteResult(
        success=True,
        record_id="42",
        fields_written=["industry"],
        status=CRMWriteState.INDETERMINATE,
        error=CRMErrorCode.TIMEOUT.value,
    )

    assert result.success is False
    assert result.fields_written == []


def test_record_reference_is_connector_scoped() -> None:
    first = CRMRecordRef("tenant-a", "portal-a", CRMType.HUBSPOT, "companies", "42")
    second = CRMRecordRef("tenant-a", "portal-b", CRMType.HUBSPOT, "companies", "42")

    assert first != second
