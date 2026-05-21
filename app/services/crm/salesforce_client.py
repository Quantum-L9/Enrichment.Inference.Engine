"""
Salesforce CRM client.

Production-ready Salesforce integration with field-level security,
bulk operations, and OAuth2 authentication.

L9 Architecture Note:
    Chassis-agnostic. Implements CRMClientBase contract.
    Never imports FastAPI. Used by WriteBackOrchestrator.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .base import CRMClientBase, CRMCredentials, WriteResult

logger = logging.getLogger(__name__)
_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]*$')


class SalesforceClient(CRMClientBase):
    """Salesforce CRM client with REST API integration."""

    def __init__(self, credentials: CRMCredentials) -> None:
        super().__init__(credentials)
        self._instance_url: str = ""
        self._access_token: str = ""
        self._api_version: str = credentials.credentials.get("api_version", "v59.0")

    @staticmethod
    def _validate_identifier(value: str, label: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError(f"Invalid Salesforce {label}: {value!r}")
        return value

    @staticmethod
    def _sanitize_soql_value(value: Any) -> str:
        text = str(value)
        text = text.replace('\\', '\\\\')
        text = text.replace("'", "\\'")
        return text

    def connect(self) -> bool:
        """Authenticate via OAuth2 password flow or JWT."""
        creds = self.credentials.credentials
        token_url = creds.get("token_url", "https://login.salesforce.com/services/oauth2/token")

        grant_type = creds.get("grant_type", "password")
        body: dict[str, str] = {
            "grant_type": grant_type,
            "client_id": creds.get("client_id", ""),
            "client_secret": creds.get("client_secret", ""),
        }

        if grant_type == "password":
            body["username"] = creds.get("username", "")
            body["password"] = creds.get("password", "") + creds.get("security_token", "")

        try:
            resp = httpx.post(token_url, data=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            self._instance_url = data["instance_url"]
            self._access_token = data["access_token"]
            logger.info("Salesforce connected: %s", self._instance_url)
            return True
        except Exception as exc:
            logger.error("Salesforce connect failed: %s", exc)
            return False

    def test_connection(self) -> bool:
        if not self._access_token:
            return False
        try:
            resp = httpx.get(
                f"{self._instance_url}/services/data/{self._api_version}/limits",
                headers=self._headers(),
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def get_record(self, object_type: str, record_id: str) -> dict[str, Any] | None:
        object_type = self._validate_identifier(object_type, 'object type')
        url = (
            f"{self._instance_url}/services/data/{self._api_version}"
            f"/sobjects/{object_type}/{record_id}"
        )
        try:
            resp = httpx.get(url, headers=self._headers(), timeout=15)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("SF get_record error: %s", exc)
            return None

    def query_records(
        self,
        object_type: str,
        filters: dict[str, Any],
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a SOQL query with validated identifiers and escaped values."""
        safe_object_type = self._validate_identifier(object_type, 'object type')

        safe_fields = [
            self._validate_identifier(field, 'field')
            for field in (fields or ['Id', 'Name'])
        ]
        field_list = ', '.join(safe_fields)

        where_parts = []
        for key, value in filters.items():
            safe_key = self._validate_identifier(key, 'filter field')
            safe_value = self._sanitize_soql_value(value)
            where_parts.append(f"{safe_key} = '{safe_value}'")

        where_clause = ' AND '.join(where_parts) if where_parts else 'Id != null'
        soql = f"SELECT {field_list} FROM {safe_object_type} WHERE {where_clause}"

        url = f"{self._instance_url}/services/data/{self._api_version}/query"
        try:
            resp = httpx.get(url, params={"q": soql}, headers=self._headers(), timeout=30)
            resp.raise_for_status()
            return resp.json().get("records", [])
        except Exception as exc:
            logger.error("SF query error: %s", exc)
            return []
