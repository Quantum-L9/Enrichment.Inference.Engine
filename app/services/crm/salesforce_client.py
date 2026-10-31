"""Salesforce CRM adapter with safe SOQL reads and permit-bound R1 updates."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import httpx
import structlog

from .base import (
    CRMClientBase,
    CRMCredentials,
    CRMErrorCode,
    CRMReadError,
    CRMRecord,
    CRMRecordRef,
    CRMWritePermit,
    CRMWriteState,
    WriteResult,
)

logger = structlog.get_logger("salesforce_client")

# SOQL field names and object types may only contain alphanumerics, underscores,
# and dots (for relationship traversal, for example Account.Name).
_SOQL_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_SALESFORCE_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")


def _soql_escape(value: str) -> str:
    """Escape a SOQL string literal, escaping backslashes before single quotes."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _validate_soql_identifiers(names: Iterable[str], kind: str) -> None:
    """Reject any interpolated SOQL token that is not a simple identifier."""
    for name in names:
        if not _SOQL_FIELD_RE.match(name):
            raise ValueError(f"Invalid SOQL {kind} '{name}': must match [A-Za-z_][A-Za-z0-9_.]*")


def _soql_literal(value: Any) -> str:
    """Return the safe canonical SOQL literal for a supported filter value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "null"
    return _soql_escape(str(value))


class SalesforceClient(CRMClientBase):
    """Salesforce REST adapter; only journaled update-by-id is enabled in R1."""

    def __init__(self, credentials: CRMCredentials) -> None:
        super().__init__(credentials)
        self._instance_url: str = ""
        self._access_token: str = ""
        self._api_version: str = credentials.credentials.get("api_version", "v59.0")

    def connect(self) -> bool:
        """Authenticate via OAuth2 without logging the returned access token."""
        credentials = self.credentials.credentials
        token_url = credentials.get(
            "token_url", "https://login.salesforce.com/services/oauth2/token"
        )
        grant_type = credentials.get("grant_type", "password")
        body: dict[str, str] = {
            "grant_type": grant_type,
            "client_id": credentials.get("client_id", ""),
            "client_secret": credentials.get("client_secret", ""),
        }
        if grant_type == "password":
            body["username"] = credentials.get("username", "")
            body["password"] = credentials.get("password", "") + credentials.get(
                "security_token", ""
            )
        try:
            response = httpx.post(token_url, data=body, timeout=30)
            if response.status_code < 200 or response.status_code >= 300:
                logger.warning("salesforce_connect_failed", status_code=response.status_code)
                return False
            result = response.json()
            if not isinstance(result, dict):
                logger.warning("salesforce_connect_failed", reason="malformed_response")
                return False
            instance_url = result.get("instance_url")
            access_token = result.get("access_token")
            if not isinstance(instance_url, str) or not isinstance(access_token, str):
                logger.warning("salesforce_connect_failed", reason="missing_auth_fields")
                return False
            self._instance_url = instance_url.rstrip("/")
            self._access_token = access_token
            logger.info("salesforce_connected")
            return True
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("salesforce_connect_failed", error_type=type(exc).__name__)
            return False

    def test_connection(self) -> bool:
        """Verify the existing Salesforce token without attempting a fresh login."""
        if not self._access_token:
            return False
        try:
            response = httpx.get(
                f"{self._instance_url}/services/data/{self._api_version}/limits",
                headers=self._headers(),
                timeout=10,
            )
            return response.status_code == 200
        except httpx.HTTPError as exc:
            logger.warning("salesforce_connection_test_failed", error_type=type(exc).__name__)
            return False

    def normalize_record_id(self, record_id: object) -> str:
        """Validate and normalize Salesforce's 15- or 18-character opaque record ID."""
        normalized = super().normalize_record_id(record_id)
        if not _SALESFORCE_RECORD_ID_RE.fullmatch(normalized):
            raise CRMReadError(
                CRMErrorCode.INVALID_IDENTIFIER,
                "Salesforce record id must be a 15- or 18-character alphanumeric identifier",
            )
        return normalized

    def normalize_record(
        self,
        tenant_id: str,
        connector_id: str,
        object_type: str,
        record: dict[str, Any],
    ) -> CRMRecord:
        """Normalize a Salesforce record while retaining its provider-native fields."""
        record_id = self.normalize_record_id(record.get("Id"))
        return CRMRecord(
            record_ref=CRMRecordRef(
                tenant_id=tenant_id,
                connector_id=connector_id,
                crm_type=self.credentials.crm_type,
                object_type=object_type,
                record_id=record_id,
            ),
            properties=dict(record),
            version_or_etag=None,
        )

    def get_record(self, object_type: str, record_id: str) -> dict[str, Any] | None:
        """Fetch one record; HTTP 404 is the only absence result."""
        _validate_soql_identifiers([object_type], "object type")
        normalized_id = self.normalize_record_id(record_id)
        url = (
            f"{self._instance_url}/services/data/{self._api_version}"
            f"/sobjects/{object_type}/{normalized_id}"
        )
        try:
            response = httpx.get(url, headers=self._headers(), timeout=15)
        except httpx.TimeoutException as exc:
            raise CRMReadError(
                CRMErrorCode.TIMEOUT,
                "Salesforce record read timed out",
                retriable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise CRMReadError(
                CRMErrorCode.TRANSPORT,
                "Salesforce record read failed",
                retriable=True,
            ) from exc
        if response.status_code == 404:
            return None
        self._raise_for_read_response(response, "Salesforce record read failed")
        result = self._json_object(response, "Salesforce record response is malformed")
        self.normalize_record("read", "read", object_type, result)
        return result

    def query_records(
        self,
        object_type: str,
        filters: dict[str, Any],
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Run a complete, injection-safe SOQL query or raise CRMReadError.

        A response that indicates additional pages without a valid next URL is
        incomplete, and therefore cannot establish absence or uniqueness.
        """
        _validate_soql_identifiers([object_type], "object type")
        _validate_soql_identifiers(fields or [], "field name")
        _validate_soql_identifiers(filters, "field name")
        field_list = ", ".join(fields) if fields else "Id, Name"
        where_parts = [f"{field} = {_soql_literal(value)}" for field, value in filters.items()]
        where_clause = " AND ".join(where_parts) if where_parts else "Id != null"
        soql = f"SELECT {field_list} FROM {object_type} WHERE {where_clause}"
        url = f"{self._instance_url}/services/data/{self._api_version}/query"
        parameters: dict[str, str] | None = {"q": soql}
        records: list[dict[str, Any]] = []
        seen_next_urls: set[str] = set()

        while True:
            try:
                response = httpx.get(url, params=parameters, headers=self._headers(), timeout=30)
            except httpx.TimeoutException as exc:
                raise CRMReadError(
                    CRMErrorCode.TIMEOUT,
                    "Salesforce record query timed out",
                    retriable=True,
                ) from exc
            except httpx.HTTPError as exc:
                raise CRMReadError(
                    CRMErrorCode.TRANSPORT,
                    "Salesforce record query failed",
                    retriable=True,
                ) from exc
            self._raise_for_read_response(response, "Salesforce record query failed")
            page = self._json_object(response, "Salesforce query response is malformed")
            page_records = page.get("records")
            if not isinstance(page_records, list) or not all(
                isinstance(record, dict) for record in page_records
            ):
                raise CRMReadError(
                    CRMErrorCode.MALFORMED_RESPONSE,
                    "Salesforce query records are malformed",
                )
            records.extend(dict(record) for record in page_records)

            done = page.get("done")
            if done is True:
                return records
            if done is not False:
                raise CRMReadError(
                    CRMErrorCode.PAGINATION_INCOMPLETE,
                    "Salesforce query does not declare page completion",
                )
            next_path = page.get("nextRecordsUrl")
            if not isinstance(next_path, str) or not next_path.startswith("/"):
                raise CRMReadError(
                    CRMErrorCode.PAGINATION_INCOMPLETE,
                    "Salesforce query next page URL is missing or unsafe",
                )
            if next_path in seen_next_urls:
                raise CRMReadError(
                    CRMErrorCode.PAGINATION_INCOMPLETE,
                    "Salesforce query repeated a page URL",
                )
            seen_next_urls.add(next_path)
            url = f"{self._instance_url}{next_path}"
            parameters = None

    def create_record(self, object_type: str, data: dict[str, Any]) -> WriteResult:
        """Refuse create because only existing-record updates are permitted in R1."""
        return self._unsafe_operation_result("create")

    def update_record(
        self,
        object_type: str,
        record_id: str,
        data: dict[str, Any],
        *,
        permit: CRMWritePermit | None = None,
    ) -> WriteResult:
        """Perform exactly a permit-bound Salesforce PATCH with a documented 204 acknowledgement."""
        _validate_soql_identifiers([object_type], "object type")
        normalized_id = self.normalize_record_id(record_id)
        denied = self.validate_update_permit(object_type, normalized_id, data, permit)
        if denied is not None:
            return denied
        if not data:
            return WriteResult(
                success=False,
                record_id=normalized_id,
                error="empty_payload",
                status=CRMWriteState.REJECTED,
            )
        url = (
            f"{self._instance_url}/services/data/{self._api_version}"
            f"/sobjects/{object_type}/{normalized_id}"
        )
        try:
            response = httpx.patch(url, json=dict(data), headers=self._headers(), timeout=15)
        except httpx.TimeoutException as exc:
            logger.warning("salesforce_update_indeterminate", error_type=type(exc).__name__)
            return self._indeterminate_result(normalized_id, CRMErrorCode.TIMEOUT)
        except httpx.HTTPError as exc:
            logger.warning("salesforce_update_indeterminate", error_type=type(exc).__name__)
            return self._indeterminate_result(normalized_id, CRMErrorCode.TRANSPORT)

        if response.status_code == 204:
            return WriteResult(
                success=True,
                record_id=normalized_id,
                fields_written=list(data.keys()),
                status=CRMWriteState.CONFIRMED,
                provider_ack="http:204",
                reconciliation_state="not_verified",
            )
        if response.status_code >= 500:
            return self._indeterminate_result(normalized_id, CRMErrorCode.REMOTE)
        return WriteResult(
            success=False,
            record_id=normalized_id,
            error=self._write_error_code(response.status_code).value,
            status=CRMWriteState.REJECTED,
            provider_ack=f"http:{response.status_code}",
        )

    def upsert_record(
        self,
        object_type: str,
        external_id_field: str,
        external_id_value: str,
        data: dict[str, Any],
    ) -> WriteResult:
        """Refuse native upsert until configured uniqueness and race semantics are proven."""
        return self._unsafe_operation_result("upsert")

    def bulk_create(self, object_type: str, records: list[dict[str, Any]]) -> list[WriteResult]:
        """Refuse bulk create; no batch may bypass the record-bound journal contract."""
        return super().bulk_create(object_type, records)

    def bulk_update(self, object_type: str, records: list[dict[str, Any]]) -> list[WriteResult]:
        """Refuse bulk update; no batch may fabricate aggregate acknowledgement success."""
        return super().bulk_update(object_type, records)

    def get_field_metadata(self, object_type: str) -> dict[str, Any]:
        """Return Salesforce describe metadata when a complete response is available."""
        _validate_soql_identifiers([object_type], "object type")
        url = f"{self._instance_url}/services/data/{self._api_version}/sobjects/{object_type}/describe"
        try:
            response = httpx.get(url, headers=self._headers(), timeout=15)
            if response.status_code < 200 or response.status_code >= 300:
                return {}
            describe = self._json_object(response, "Salesforce metadata response is malformed")
            fields = describe.get("fields")
            if not isinstance(fields, list):
                return {}
            metadata: dict[str, Any] = {}
            for field in fields:
                if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                    return {}
                metadata[field["name"]] = {
                    "type": field.get("type"),
                    "label": field.get("label"),
                    "updateable": bool(field.get("updateable", False)),
                    "createable": bool(field.get("createable", False)),
                    "nillable": bool(field.get("nillable", False)),
                    "length": field.get("length"),
                }
            return metadata
        except (httpx.HTTPError, CRMReadError):
            return {}

    def _headers(self) -> dict[str, str]:
        """Build authorization headers without exposing the access token to logs."""
        return {"Authorization": f"Bearer {self._access_token}", "Content-Type": "application/json"}

    def _raise_for_read_response(self, response: httpx.Response, message: str) -> None:
        """Translate failed read status codes without misclassifying them as absence."""
        if 200 <= response.status_code < 300:
            return
        code = self._write_error_code(response.status_code)
        raise CRMReadError(
            code,
            message,
            retriable=code in {CRMErrorCode.RATE_LIMIT, CRMErrorCode.REMOTE},
        )

    def _json_object(self, response: httpx.Response, message: str) -> dict[str, Any]:
        """Return a JSON object or a typed malformed-response error."""
        try:
            body = response.json()
        except ValueError as exc:
            raise CRMReadError(CRMErrorCode.MALFORMED_RESPONSE, message) from exc
        if not isinstance(body, dict):
            raise CRMReadError(CRMErrorCode.MALFORMED_RESPONSE, message)
        return body

    def _write_error_code(self, status_code: int) -> CRMErrorCode:
        """Map relevant Salesforce HTTP status codes to shared classifications."""
        if status_code == 401:
            return CRMErrorCode.AUTHENTICATION
        if status_code == 403:
            return CRMErrorCode.AUTHORIZATION
        if status_code == 409:
            return CRMErrorCode.CONFLICT
        if status_code == 429:
            return CRMErrorCode.RATE_LIMIT
        return CRMErrorCode.REMOTE

    def _indeterminate_result(self, record_id: str, code: CRMErrorCode) -> WriteResult:
        """Build the no-retry result for a PATCH that may have reached Salesforce."""
        return WriteResult(
            success=False,
            record_id=record_id,
            error=code.value,
            status=CRMWriteState.INDETERMINATE,
            reconciliation_state="required",
        )
