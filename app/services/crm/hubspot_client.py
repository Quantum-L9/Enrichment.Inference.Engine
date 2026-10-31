"""HubSpot CRM v3 adapter with explicit lookup and mutation acknowledgement semantics."""

from __future__ import annotations

import time
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

logger = structlog.get_logger("hubspot_client")

_OBJECT_TYPE_MAP = {
    "company": "companies",
    "contact": "contacts",
    "deal": "deals",
    "opportunity": "deals",
}


class HubSpotClient(CRMClientBase):
    """HubSpot client; R1 permits only journaled, permit-bound update-by-id."""

    def __init__(self, credentials: CRMCredentials) -> None:
        super().__init__(credentials)
        self._access_token: str = credentials.credentials.get("access_token", "")
        self._base_url: str = "https://api.hubapi.com"
        self._rate_limit_remaining: int = 100
        self._rate_limit_reset: float = 0.0

    def connect(self) -> bool:
        """Validate the access token without emitting its value to logs."""
        try:
            response = httpx.get(
                f"{self._base_url}/account-info/v3/details",
                headers=self._headers(),
                timeout=10,
            )
            if response.status_code != 200:
                logger.warning("hubspot_connect_failed", status_code=response.status_code)
                return False
            body = response.json()
            if not isinstance(body, dict):
                logger.warning("hubspot_connect_failed", reason="malformed_response")
                return False
            portal_id = str(body.get("portalId", "")).strip()
            expected_portal_id = self.credentials.provider_account_id.strip()
            if expected_portal_id and portal_id != expected_portal_id:
                logger.warning("hubspot_connect_failed", reason="provider_account_mismatch")
                return False
            logger.info("hubspot_connected", portal_id=portal_id)
            return True
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("hubspot_connect_failed", error_type=type(exc).__name__)
            return False

    def test_connection(self) -> bool:
        """Verify the configured HubSpot credentials remain usable."""
        try:
            response = httpx.get(
                f"{self._base_url}/account-info/v3/details",
                headers=self._headers(),
                timeout=10,
            )
            if response.status_code != 200:
                return False
            if not self.credentials.provider_account_id:
                return True
            body = response.json()
            return isinstance(body, dict) and str(body.get("portalId", "")).strip() == (
                self.credentials.provider_account_id.strip()
            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("hubspot_connection_test_failed", error_type=type(exc).__name__)
            return False

    def normalize_record(
        self,
        tenant_id: str,
        connector_id: str,
        object_type: str,
        record: dict[str, Any],
    ) -> CRMRecord:
        """Normalize the documented top-level HubSpot record ID and properties."""
        record_id = self.normalize_record_id(record.get("id"))
        properties = record.get("properties", {})
        if not isinstance(properties, dict):
            raise CRMReadError(
                CRMErrorCode.MALFORMED_RESPONSE, "HubSpot properties are not an object"
            )
        normalized_properties = dict(properties)
        normalized_properties["id"] = record_id
        return CRMRecord(
            record_ref=CRMRecordRef(
                tenant_id=tenant_id,
                connector_id=connector_id,
                crm_type=self.credentials.crm_type,
                object_type=object_type,
                record_id=record_id,
            ),
            properties=normalized_properties,
            version_or_etag=None,
        )

    def get_record(self, object_type: str, record_id: str) -> dict[str, Any] | None:
        """Fetch one HubSpot record; only documented HTTP 404 means not found."""
        normalized_id = self.normalize_record_id(record_id)
        api_type = _OBJECT_TYPE_MAP.get(object_type, object_type)
        url = f"{self._base_url}/crm/v3/objects/{api_type}/{normalized_id}"
        self._respect_rate_limit()
        try:
            response = httpx.get(url, headers=self._headers(), timeout=15)
        except httpx.TimeoutException as exc:
            raise CRMReadError(
                CRMErrorCode.TIMEOUT, "HubSpot record read timed out", retriable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise CRMReadError(
                CRMErrorCode.TRANSPORT, "HubSpot record read failed", retriable=True
            ) from exc
        self._update_rate_limit(response)
        if response.status_code == 404:
            return None
        self._raise_for_read_response(response, "HubSpot record read failed")
        body = self._json_object(response, "HubSpot record response is malformed")
        normalized = self.normalize_record("read", "read", object_type, body)
        return normalized.properties

    def query_records(
        self,
        object_type: str,
        filters: dict[str, Any],
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return a complete HubSpot search result with top-level IDs preserved."""
        api_type = _OBJECT_TYPE_MAP.get(object_type, object_type)
        url = f"{self._base_url}/crm/v3/objects/{api_type}/search"
        filter_groups = [
            {
                "filters": [
                    {"propertyName": key, "operator": "EQ", "value": str(value)}
                    for key, value in filters.items()
                ]
            }
        ]
        records: list[dict[str, Any]] = []
        after: str | None = None
        seen_after: set[str] = set()
        while True:
            body: dict[str, Any] = {"filterGroups": filter_groups, "limit": 100}
            if fields:
                body["properties"] = list(fields)
            if after is not None:
                body["after"] = after
            self._respect_rate_limit()
            try:
                response = httpx.post(url, json=body, headers=self._headers(), timeout=30)
            except httpx.TimeoutException as exc:
                raise CRMReadError(
                    CRMErrorCode.TIMEOUT,
                    "HubSpot record query timed out",
                    retriable=True,
                ) from exc
            except httpx.HTTPError as exc:
                raise CRMReadError(
                    CRMErrorCode.TRANSPORT,
                    "HubSpot record query failed",
                    retriable=True,
                ) from exc
            self._update_rate_limit(response)
            self._raise_for_read_response(response, "HubSpot record query failed")
            page = self._json_object(response, "HubSpot query response is malformed")
            results = page.get("results")
            if not isinstance(results, list):
                raise CRMReadError(
                    CRMErrorCode.MALFORMED_RESPONSE, "HubSpot query results are not a list"
                )
            for result in results:
                if not isinstance(result, dict):
                    raise CRMReadError(
                        CRMErrorCode.MALFORMED_RESPONSE,
                        "HubSpot query contains a non-object record",
                    )
                normalized = self.normalize_record("query", "query", object_type, result)
                records.append(normalized.properties)

            paging = page.get("paging")
            if paging is None:
                return records
            if not isinstance(paging, dict):
                raise CRMReadError(
                    CRMErrorCode.PAGINATION_INCOMPLETE,
                    "HubSpot query paging metadata is malformed",
                )
            next_page = paging.get("next")
            if not isinstance(next_page, dict):
                raise CRMReadError(
                    CRMErrorCode.PAGINATION_INCOMPLETE,
                    "HubSpot query has incomplete paging metadata",
                )
            next_after = next_page.get("after")
            if next_after is None or not str(next_after).strip():
                raise CRMReadError(
                    CRMErrorCode.PAGINATION_INCOMPLETE,
                    "HubSpot query next-page cursor is missing",
                )
            after = str(next_after).strip()
            if after in seen_after:
                raise CRMReadError(
                    CRMErrorCode.PAGINATION_INCOMPLETE,
                    "HubSpot query repeated a paging cursor",
                )
            seen_after.add(after)

    def create_record(self, object_type: str, data: dict[str, Any]) -> WriteResult:
        """Refuse direct create; it is outside the initial safe writeback release."""
        return self._unsafe_operation_result("create")

    def update_record(
        self,
        object_type: str,
        record_id: str,
        data: dict[str, Any],
        *,
        permit: CRMWritePermit | None = None,
    ) -> WriteResult:
        """Update an existing record after permit validation and documented acknowledgement."""
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
        api_type = _OBJECT_TYPE_MAP.get(object_type, object_type)
        url = f"{self._base_url}/crm/v3/objects/{api_type}/{normalized_id}"
        self._respect_rate_limit()
        try:
            response = httpx.patch(
                url,
                json={"properties": dict(data)},
                headers=self._headers(),
                timeout=15,
            )
        except httpx.TimeoutException as exc:
            logger.warning("hubspot_update_indeterminate", error_type=type(exc).__name__)
            return self._indeterminate_result(normalized_id, CRMErrorCode.TIMEOUT)
        except httpx.HTTPError as exc:
            logger.warning("hubspot_update_indeterminate", error_type=type(exc).__name__)
            return self._indeterminate_result(normalized_id, CRMErrorCode.TRANSPORT)
        self._update_rate_limit(response)
        if response.status_code >= 500:
            return self._indeterminate_result(normalized_id, CRMErrorCode.REMOTE)
        if response.status_code < 200 or response.status_code >= 300:
            return WriteResult(
                success=False,
                record_id=normalized_id,
                error=self._write_error_code(response.status_code).value,
                status=CRMWriteState.REJECTED,
                provider_ack=f"http:{response.status_code}",
            )
        try:
            body = self._json_object(response, "HubSpot update acknowledgement is malformed")
            acknowledged_id = self.normalize_record_id(body.get("id"))
        except CRMReadError as exc:
            return self._indeterminate_result(
                normalized_id,
                exc.code,
                provider_ack=f"http:{response.status_code}",
            )
        if acknowledged_id != normalized_id:
            return self._indeterminate_result(
                normalized_id,
                CRMErrorCode.CONFLICT,
                provider_ack=f"http:{response.status_code}",
            )
        return WriteResult(
            success=True,
            record_id=normalized_id,
            fields_written=list(data.keys()),
            status=CRMWriteState.CONFIRMED,
            provider_ack=f"http:{response.status_code}",
            reconciliation_state="not_verified",
        )

    def upsert_record(
        self,
        object_type: str,
        external_id_field: str,
        external_id_value: str,
        data: dict[str, Any],
    ) -> WriteResult:
        """Refuse lookup-then-create upsert until uniqueness and replay rules are proven."""
        return self._unsafe_operation_result("upsert")

    def bulk_create(self, object_type: str, records: list[dict[str, Any]]) -> list[WriteResult]:
        """Refuse bulk create because it cannot carry one durable record-level receipt per item."""
        return super().bulk_create(object_type, records)

    def bulk_update(self, object_type: str, records: list[dict[str, Any]]) -> list[WriteResult]:
        """Refuse bulk update, preserving caller-owned inputs without mutation."""
        return super().bulk_update(object_type, records)

    def get_field_metadata(self, object_type: str) -> dict[str, Any]:
        """Return HubSpot metadata when a complete valid metadata response is available."""
        api_type = _OBJECT_TYPE_MAP.get(object_type, object_type)
        url = f"{self._base_url}/crm/v3/properties/{api_type}"
        try:
            response = httpx.get(url, headers=self._headers(), timeout=15)
            if response.status_code < 200 or response.status_code >= 300:
                return {}
            body = self._json_object(response, "HubSpot metadata response is malformed")
            results = body.get("results")
            if not isinstance(results, list):
                return {}
            metadata: dict[str, Any] = {}
            for field in results:
                if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                    return {}
                metadata[field["name"]] = {
                    "type": field.get("type"),
                    "label": field.get("label"),
                    "field_type": field.get("fieldType"),
                    "group_name": field.get("groupName"),
                    "has_unique_value": bool(field.get("hasUniqueValue", False)),
                }
            return metadata
        except (httpx.HTTPError, CRMReadError):
            return {}

    def _headers(self) -> dict[str, str]:
        """Build authorization headers without exposing the token outside the request."""
        return {"Authorization": f"Bearer {self._access_token}", "Content-Type": "application/json"}

    def _respect_rate_limit(self) -> None:
        """Wait only for a provider-supplied remaining/reset limit."""
        if self._rate_limit_remaining < 5:
            wait = max(0.0, self._rate_limit_reset - time.time())
            if wait > 0:
                logger.info("hubspot_rate_limit_wait", wait_seconds=round(wait, 3))
                time.sleep(wait)

    def _update_rate_limit(self, response: httpx.Response) -> None:
        """Update local advisory rate-limit tracking without making header parse failures fatal."""
        remaining = response.headers.get("X-HubSpot-RateLimit-Remaining")
        if remaining is not None:
            try:
                self._rate_limit_remaining = int(remaining)
            except ValueError:
                logger.warning("hubspot_rate_limit_header_invalid", header="remaining")
        interval = response.headers.get("X-HubSpot-RateLimit-Interval-Milliseconds")
        if interval is not None:
            try:
                self._rate_limit_reset = time.time() + int(interval) / 1000.0
            except ValueError:
                logger.warning("hubspot_rate_limit_header_invalid", header="interval")

    def _raise_for_read_response(self, response: httpx.Response, message: str) -> None:
        """Translate HTTP read failures to a non-absence CRMReadError."""
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
        """Map documented HTTP classes without treating an error as an empty result."""
        if status_code == 401:
            return CRMErrorCode.AUTHENTICATION
        if status_code == 403:
            return CRMErrorCode.AUTHORIZATION
        if status_code == 409:
            return CRMErrorCode.CONFLICT
        if status_code == 429:
            return CRMErrorCode.RATE_LIMIT
        return CRMErrorCode.REMOTE

    def _indeterminate_result(
        self,
        record_id: str,
        code: CRMErrorCode,
        *,
        provider_ack: str | None = None,
    ) -> WriteResult:
        """Build the no-retry result for a mutation whose remote commit may be unknown."""
        return WriteResult(
            success=False,
            record_id=record_id,
            error=code.value,
            status=CRMWriteState.INDETERMINATE,
            provider_ack=provider_ack,
            reconciliation_state="required",
        )
