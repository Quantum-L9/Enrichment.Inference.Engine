"""
Odoo CRM client — XML-RPC interface with async wrappers.

Uses Odoo's standard external API (xmlrpc.client) with asyncio.to_thread
for compatibility with the async enrichment pipeline.

L9 Architecture Note:
    This module is chassis-agnostic. It never imports FastAPI.
    It is called by WriteBackOrchestrator, which is called by handlers.py.
"""

from __future__ import annotations

import asyncio
import xmlrpc.client
from typing import Any

import structlog

from .base import CRMClientBase, CRMCredentials, WriteResult

logger = structlog.get_logger("odoo_client")


class OdooClient(CRMClientBase):
    """
    Odoo CRM client using the standard XML-RPC external API.

    All blocking xmlrpc calls are wrapped in asyncio.to_thread for
    compatibility with async callers.
    """

    def __init__(self, credentials: CRMCredentials) -> None:
        super().__init__(credentials)
        creds = credentials.credentials
        self.url: str = creds["url"].rstrip("/")
        self.db: str = creds["db"]
        self.username: str = creds["username"]
        self.password: str = creds["password"]
        self._uid: int | None = None

        self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")

    # ── Connection ────────────────────────────────────────────

    def connect(self) -> bool:
        """Authenticate with Odoo and cache the UID."""
        try:
            raw_uid = self._common.authenticate(self.db, self.username, self.password, {})
            # Odoo returns an int uid on success or False on failure; normalize to int | None.
            self._uid = (
                raw_uid if isinstance(raw_uid, int) and not isinstance(raw_uid, bool) else None
            )
            if not self._uid:
                logger.error("odoo_auth_failed", reason="falsy_uid")
                return False
            logger.info("odoo_connected", uid=self._uid)
            return True
        except Exception as exc:
            logger.error("odoo_connect_failed", error=str(exc))
            return False

    def test_connection(self) -> bool:
        """Verify the connection is still alive."""
        if not self._uid:
            return self.connect()
        try:
            result = self._execute("res.users", "search", [[["id", "=", self._uid]]])
            return bool(result)
        except Exception:
            return False

    # ── CRUD ──────────────────────────────────────────────────

    def get_record(self, object_type: str, record_id: str) -> dict[str, Any] | None:
        """Fetch a single record by ID."""
        try:
            results = self._execute(object_type, "read", [[int(record_id)]])
            return results[0] if results else None
        except Exception as exc:
            logger.error(
                "odoo_get_record_failed",
                object_type=object_type,
                record_id=record_id,
                error=str(exc),
            )
            return None

    def query_records(
        self,
        object_type: str,
        filters: dict[str, Any],
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Query records matching filters."""
        domain = [[k, "=", v] for k, v in filters.items()]
        try:
            ids = self._execute(object_type, "search", [domain])
            if not ids:
                return []
            kwargs: dict[str, Any] = {}
            if fields:
                kwargs["fields"] = fields
            records: list[dict[str, Any]] = self._execute(object_type, "read", [ids], **kwargs)
            return records
        except Exception as exc:
            logger.error(
                "odoo_query_failed",
                object_type=object_type,
                error=str(exc),
            )
            return []

    def create_record(self, object_type: str, data: dict[str, Any]) -> WriteResult:
        """Create a new record in Odoo."""
        try:
            record_id = self._execute(object_type, "create", [data])
            logger.info(
                "odoo_record_created",
                object_type=object_type,
                record_id=record_id,
                fields=list(data.keys()),
            )
            return WriteResult(
                success=True,
                record_id=str(record_id),
                fields_written=list(data.keys()),
            )
        except Exception as exc:
            logger.error(
                "odoo_create_failed",
                object_type=object_type,
                error=str(exc),
            )
            return WriteResult(success=False, error=str(exc))

    def update_record(self, object_type: str, record_id: str, data: dict[str, Any]) -> WriteResult:
        """Update an existing record in Odoo."""
        try:
            self._execute(object_type, "write", [[int(record_id)], data])
            logger.info(
                "odoo_record_updated",
                object_type=object_type,
                record_id=record_id,
                fields=list(data.keys()),
            )
            return WriteResult(
                success=True,
                record_id=record_id,
                fields_written=list(data.keys()),
            )
        except Exception as exc:
            logger.error(
                "odoo_update_failed",
                object_type=object_type,
                record_id=record_id,
                error=str(exc),
            )
            return WriteResult(success=False, error=str(exc))

    def upsert_record(
        self,
        object_type: str,
        external_id_field: str,
        external_id_value: str,
        data: dict[str, Any],
    ) -> WriteResult:
        """Create or update based on external ID lookup."""
        existing = self.query_records(
            object_type,
            {external_id_field: external_id_value},
            fields=["id"],
        )
        if existing:
            rid = str(existing[0]["id"])
            return self.update_record(object_type, rid, data)
        data[external_id_field] = external_id_value
        return self.create_record(object_type, data)

    def get_field_metadata(self, object_type: str) -> dict[str, Any]:
        """Return field schema metadata for an Odoo model."""
        try:
            fields = self._execute(
                object_type,
                "fields_get",
                [],
                attributes=["string", "type", "required"],
            )
            return {
                "name": object_type,
                "fields": [{"name": k, **v} for k, v in fields.items()],
            }
        except Exception as exc:
            logger.error(
                "odoo_field_metadata_failed",
                object_type=object_type,
                error=str(exc),
            )
            return {}

    # ── Async wrappers ────────────────────────────────────────

    async def async_connect(self) -> bool:
        """Async wrapper for connect()."""
        return await asyncio.to_thread(self.connect)

    async def async_create_record(self, object_type: str, data: dict[str, Any]) -> WriteResult:
        """Async wrapper for create_record()."""
        return await asyncio.to_thread(self.create_record, object_type, data)

    async def async_update_record(
        self, object_type: str, record_id: str, data: dict[str, Any]
    ) -> WriteResult:
        """Async wrapper for update_record()."""
        return await asyncio.to_thread(self.update_record, object_type, record_id, data)

    async def async_upsert_record(
        self,
        object_type: str,
        external_id_field: str,
        external_id_value: str,
        data: dict[str, Any],
    ) -> WriteResult:
        """Async wrapper for upsert_record()."""
        return await asyncio.to_thread(
            self.upsert_record,
            object_type,
            external_id_field,
            external_id_value,
            data,
        )

    # ── Internal ──────────────────────────────────────────────

    def _execute(
        self,
        model: str,
        method: str,
        args: list[Any],
        **kwargs: Any,
    ) -> Any:
        """Execute an Odoo XML-RPC call."""
        if not self._uid:
            msg = "Not authenticated — call connect() first"
            raise RuntimeError(msg)
        return self._models.execute_kw(
            self.db,
            self._uid,
            self.password,
            model,
            method,
            args,
            kwargs,
        )
