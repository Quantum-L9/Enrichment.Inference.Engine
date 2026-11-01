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
import re
import time
from dataclasses import dataclass
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

# Date-versioned CRM API (HubSpot retires the semantic v4 associations API in
# March 2027). One page is read; a record with more is refused, never truncated.
_ASSOCIATIONS_API_VERSION = "2026-09"
# Standard names (``companies``) and object type ids (``0-3``, ``2-1234``).
_OBJECT_TYPE_SEGMENT = re.compile(r"[a-z][a-z0-9_]{0,63}|[0-9]+-[0-9]+")
# A canonical HubSpot record id: ASCII digits, no leading zero.
_RECORD_ID = re.compile(r"[1-9][0-9]{0,30}")


def native_object_type(object_type: str) -> str:
    """The provider-native object type for an EIE alias (``company`` -> ``companies``); else unchanged."""
    return _OBJECT_TYPE_MAP.get(object_type, object_type)


def is_canonical_record_id(value: object) -> bool:
    """Whether ``value`` is a record id exactly as HubSpot renders it (``str(int)``)."""
    return isinstance(value, str) and _RECORD_ID.fullmatch(value) is not None


class HubSpotAssociationReadError(RuntimeError):
    """An association read could not prove what the source holds.

    Raised for any non-200 answer (403 missing scope, 404 unknown record, 429
    rate limit, 5xx outage), a transport failure, a malformed body or a result
    that does not fit one page. Unlike the writeback methods, the association
    read never answers "no associations" for a failure.
    """

    def __init__(self, reason: str, *, status_code: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class HubSpotAssociation:
    """One association type between two records, exactly as HubSpot returned it."""

    from_object_type: str
    from_record_id: str
    to_object_type: str
    to_record_id: str
    category: str
    type_id: int
    label: str | None


def _parse_association_types(
    from_object_type: str,
    from_record_id: str,
    to_object_type: str,
    entry: Any,
) -> list[HubSpotAssociation]:
    if not isinstance(entry, dict):
        raise HubSpotAssociationReadError("malformed association entry")
    to_id = entry.get("toObjectId")
    types = entry.get("associationTypes")
    if isinstance(to_id, bool) or not isinstance(to_id, int | str) or not str(to_id).strip():
        raise HubSpotAssociationReadError("association entry has no toObjectId")
    if not isinstance(types, list) or not types:
        raise HubSpotAssociationReadError("association entry has no associationTypes")
    parsed = []
    for association_type in types:
        if not isinstance(association_type, dict):
            raise HubSpotAssociationReadError("malformed association type")
        category = association_type.get("category")
        type_id = association_type.get("typeId")
        label = association_type.get("label")
        if not isinstance(category, str) or not category:
            raise HubSpotAssociationReadError("association type has no category")
        if isinstance(type_id, bool) or not isinstance(type_id, int):
            raise HubSpotAssociationReadError("association type has no integer typeId")
        if label is not None and not isinstance(label, str):
            raise HubSpotAssociationReadError("association type label is not a string")
        parsed.append(
            HubSpotAssociation(
                from_object_type=from_object_type,
                from_record_id=from_record_id,
                to_object_type=to_object_type,
                to_record_id=str(to_id),
                category=category,
                type_id=type_id,
                label=label,
            )
        )
    return parsed


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
            logger.exception("HubSpot connect failed: %s", exc)
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
            logger.exception("HS get_record error: %s", exc)
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
            logger.exception("HS query error: %s", exc)
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

    def read_associations(
        self,
        from_object_type: str,
        from_record_id: str,
        to_object_type: str,
    ) -> list[HubSpotAssociation]:
        """Read one record's associations to one object type. Read-only.

        ``GET /crm/objects/2026-09/{from}/{id}/associations/{to}`` (the
        date-versioned successor of the v4 associations read; same response
        shape). Every association type HubSpot reports is returned with its
        category, numeric ``typeId`` and label. Any failure raises
        ``HubSpotAssociationReadError``; an empty list means HubSpot answered
        200 with one complete page holding no associations.
        """
        from_type = native_object_type(from_object_type)
        to_type = native_object_type(to_object_type)
        # Path segments: an object type name or id must never reshape the URL.
        if not (
            _OBJECT_TYPE_SEGMENT.fullmatch(from_type) and _OBJECT_TYPE_SEGMENT.fullmatch(to_type)
        ):
            raise HubSpotAssociationReadError(
                "object type must be a plain HubSpot object type name"
            )
        if not is_canonical_record_id(from_record_id):
            raise HubSpotAssociationReadError("record id must be a canonical numeric HubSpot id")
        url = (
            f"{self._base_url}/crm/objects/{_ASSOCIATIONS_API_VERSION}/"
            f"{from_type}/{from_record_id}/associations/{to_type}"
        )

        self._respect_rate_limit()
        try:
            resp = httpx.get(url, headers=self._headers(), timeout=15)
        except httpx.HTTPError as exc:
            raise HubSpotAssociationReadError(f"transport failure: {type(exc).__name__}") from exc
        # Association evidence refuses a read whose rate-limit headers cannot be
        # parsed. CRM advisory tracking may swallow the same headers; this check
        # stays in front of that helper so a bad header cannot look complete.
        try:
            for header_name in (
                "X-HubSpot-RateLimit-Remaining",
                "X-HubSpot-RateLimit-Interval-Milliseconds",
            ):
                header_value = resp.headers.get(header_name)
                if header_value is not None:
                    int(header_value)
        except ValueError as exc:
            raise HubSpotAssociationReadError("malformed rate-limit headers") from exc
        self._update_rate_limit(resp)
        if resp.status_code != 200:
            raise HubSpotAssociationReadError(
                f"HubSpot answered {resp.status_code}", status_code=resp.status_code
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise HubSpotAssociationReadError("response body is not JSON") from exc
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise HubSpotAssociationReadError("response has no results list")
        # Completeness must be proven: any paging object that is not a dict, or
        # that carries a ``next`` key at all, means the page may not be the last.
        if "paging" in body:
            paging = body["paging"]
            if not isinstance(paging, dict) or "next" in paging:
                raise HubSpotAssociationReadError(
                    "associations span more than one page or paging is malformed; "
                    "refusing an unproven-complete read"
                )
        associations: list[HubSpotAssociation] = []
        for entry in results:
            associations.extend(
                _parse_association_types(from_type, str(from_record_id), to_type, entry)
            )
        return associations

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
            logger.exception("HS metadata error: %s", exc)
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
