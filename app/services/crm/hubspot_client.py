"""
HubSpot CRM client.

Production-ready HubSpot integration with rate limiting,
batch operations, and property group management.

L9 Architecture Note:
    Chassis-agnostic. Implements CRMClientBase contract.
    Never imports FastAPI. Used by WriteBackOrchestrator.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .base import CRMClientBase, CRMCredentials, WriteResult

logger = logging.getLogger(__name__)

_OBJECT_TYPE_MAP = {
    "company": "companies",
    "contact": "contacts",
    "deal": "deals",
    "opportunity": "deals",
}


class HubSpotClient(CRMClientBase):
    """HubSpot CRM client with REST API v3 integration."""

    def __init__(self, credentials: CRMCredentials) -> None:
        super().__init__(credentials)
        self._access_token: str = credentials.credentials.get("access_token", "")
        self._base_url: str = "https://api.hubapi.com"
        self._rate_limit_remaining: int = 100
        self._rate_limit_reset: float = 0.0

    def connect(self) -> bool:
        """Validate the access token by fetching account info."""
        try:
            resp = httpx.get(
                f"{self._base_url}/account-info/v3/details",
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
            info = resp.json()
            logger.info("HubSpot connected: portal %s", info.get("portalId"))
            return True
        except Exception as exc:
            logger.error("HubSpot connect failed: %s", exc)
            return False

    def test_connection(self) -> bool:
        """Verify the connection is still alive."""
        try:
            resp = httpx.get(
                f"{self._base_url}/account-info/v3/details",
                headers=self._headers(),
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def get_record(self, object_type: str, record_id: str) -> dict[str, Any] | None:
        """Fetch a single HubSpot record by ID."""
        api_type = _OBJECT_TYPE_MAP.get(object_type, object_type)
        url = f"{self._base_url}/crm/v3/objects/{api_type}/{record_id}"

        self._respect_rate_limit()
        try:
            resp = httpx.get(url, headers=self._headers(), timeout=15)
            self._update_rate_limit(resp)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            properties: dict[str, Any] = resp.json().get("properties", {})
            return properties
        except Exception as exc:
            logger.error("HS get_record error: %s", exc)
            return None

    def query_records(
        self,
        object_type: str,
        filters: dict[str, Any],
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search HubSpot records using filter groups."""
        api_type = _OBJECT_TYPE_MAP.get(object_type, object_type)
        url = f"{self._base_url}/crm/v3/objects/{api_type}/search"

        filter_groups = [
            {
                "filters": [
                    {
                        "propertyName": k,
                        "operator": "EQ",
                        "value": str(v),
                    }
                    for k, v in filters.items()
                ]
            }
        ]

        body: dict[str, Any] = {"filterGroups": filter_groups, "limit": 100}
        if fields:
            body["properties"] = fields

        self._respect_rate_limit()
        try:
            resp = httpx.post(url, json=body, headers=self._headers(), timeout=30)
            self._update_rate_limit(resp)
            resp.raise_for_status()
            return [r.get("properties", {}) for r in resp.json().get("results", [])]
        except Exception as exc:
            logger.error("HS query error: %s", exc)
            return []

    def create_record(self, object_type: str, data: dict[str, Any]) -> WriteResult:
        """Create a new HubSpot record."""
        api_type = _OBJECT_TYPE_MAP.get(object_type, object_type)
        url = f"{self._base_url}/crm/v3/objects/{api_type}"

        self._respect_rate_limit()
        try:
            resp = httpx.post(
                url,
                json={"properties": data},
                headers=self._headers(),
                timeout=15,
            )
            self._update_rate_limit(resp)
            resp.raise_for_status()
            result = resp.json()
            return WriteResult(
                success=True,
                record_id=result.get("id", ""),
                fields_written=list(data.keys()),
            )
        except Exception as exc:
            return WriteResult(success=False, error=str(exc))

    def update_record(self, object_type: str, record_id: str, data: dict[str, Any]) -> WriteResult:
        """Update an existing HubSpot record."""
        api_type = _OBJECT_TYPE_MAP.get(object_type, object_type)
        url = f"{self._base_url}/crm/v3/objects/{api_type}/{record_id}"

        self._respect_rate_limit()
        try:
            resp = httpx.patch(
                url,
                json={"properties": data},
                headers=self._headers(),
                timeout=15,
            )
            self._update_rate_limit(resp)
            resp.raise_for_status()
            return WriteResult(
                success=True,
                record_id=record_id,
                fields_written=list(data.keys()),
            )
        except Exception as exc:
            return WriteResult(success=False, error=str(exc))

    def upsert_record(
        self,
        object_type: str,
        external_id_field: str,
        external_id_value: str,
        data: dict[str, Any],
    ) -> WriteResult:
        """Upsert: search by external ID, then create or update."""
        existing = self.query_records(
            object_type,
            {external_id_field: external_id_value},
        )
        if existing:
            record_id = existing[0].get("hs_object_id", "")
            if record_id:
                return self.update_record(object_type, record_id, data)
        return self.create_record(object_type, data)

    def bulk_create(self, object_type: str, records: list[dict[str, Any]]) -> list[WriteResult]:
        """Create multiple records using HubSpot batch API."""
        api_type = _OBJECT_TYPE_MAP.get(object_type, object_type)
        url = f"{self._base_url}/crm/v3/objects/{api_type}/batch/create"

        inputs = [{"properties": rec} for rec in records]

        self._respect_rate_limit()
        try:
            resp = httpx.post(url, json={"inputs": inputs}, headers=self._headers(), timeout=60)
            self._update_rate_limit(resp)
            resp.raise_for_status()
            results = resp.json().get("results", [])
            return [
                WriteResult(
                    success=True,
                    record_id=r.get("id", ""),
                    fields_written=list(records[i].keys()) if i < len(records) else [],
                )
                for i, r in enumerate(results)
            ]
        except Exception as exc:
            return [WriteResult(success=False, error=str(exc))] * len(records)

    def bulk_update(self, object_type: str, records: list[dict[str, Any]]) -> list[WriteResult]:
        """Update multiple records using HubSpot batch API."""
        api_type = _OBJECT_TYPE_MAP.get(object_type, object_type)
        url = f"{self._base_url}/crm/v3/objects/{api_type}/batch/update"

        inputs = [{"id": rec.pop("id", ""), "properties": rec} for rec in records]

        self._respect_rate_limit()
        try:
            resp = httpx.post(url, json={"inputs": inputs}, headers=self._headers(), timeout=60)
            self._update_rate_limit(resp)
            resp.raise_for_status()
            results = resp.json().get("results", [])
            return [
                WriteResult(
                    success=True,
                    record_id=r.get("id", ""),
                    fields_written=list(r.get("properties", {}).keys()),
                )
                for r in results
            ]
        except Exception as exc:
            return [WriteResult(success=False, error=str(exc))] * len(records)

    def get_field_metadata(self, object_type: str) -> dict[str, Any]:
        """Return field schema metadata for a HubSpot object."""
        api_type = _OBJECT_TYPE_MAP.get(object_type, object_type)
        url = f"{self._base_url}/crm/v3/properties/{api_type}"

        try:
            resp = httpx.get(url, headers=self._headers(), timeout=15)
            resp.raise_for_status()
            return {
                p["name"]: {
                    "type": p["type"],
                    "label": p["label"],
                    "field_type": p.get("fieldType"),
                    "group_name": p.get("groupName"),
                    "has_unique_value": p.get("hasUniqueValue", False),
                }
                for p in resp.json().get("results", [])
            }
        except Exception as exc:
            logger.error("HS metadata error: %s", exc)
            return {}

    def _headers(self) -> dict[str, str]:
        """Build authorization headers."""
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    def _respect_rate_limit(self) -> None:
        """Wait if we are near the rate limit."""
        if self._rate_limit_remaining < 5:
            wait = max(0, self._rate_limit_reset - time.time())
            if wait > 0:
                logger.info("HubSpot rate limit: waiting %.1fs", wait)
                time.sleep(wait)

    def _update_rate_limit(self, resp: httpx.Response) -> None:
        """Update rate limit tracking from response headers."""
        remaining = resp.headers.get("X-HubSpot-RateLimit-Remaining")
        if remaining is not None:
            self._rate_limit_remaining = int(remaining)
        interval = resp.headers.get("X-HubSpot-RateLimit-Interval-Milliseconds")
        if interval is not None:
            self._rate_limit_reset = time.time() + int(interval) / 1000.0
