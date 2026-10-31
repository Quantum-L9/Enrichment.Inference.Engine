"""Odoo CRM adapter with explicit read and acknowledgement semantics."""

from __future__ import annotations

import asyncio
import importlib
import socket
from typing import Any

import structlog
from defusedxml import xmlrpc as defused_xmlrpc

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

logger = structlog.get_logger("odoo_client")

# Odoo responses are remote XML. Patch the standard XML-RPC parser before the
# client module is loaded so entity expansion and gzip decompression limits are
# enforced for every XML-RPC response in this process.
defused_xmlrpc.monkey_patch()
xmlrpc_client = importlib.import_module("xmlrpc.client")


class OdooClient(CRMClientBase):
    """Odoo XML-RPC adapter; only permit-bound update-by-id is mutating in R1."""

    def __init__(self, credentials: CRMCredentials) -> None:
        super().__init__(credentials)
        creds = credentials.credentials
        self.url: str = creds["url"].rstrip("/")
        self.db: str = creds["db"]
        self.username: str = creds["username"]
        self.password: str = creds["password"]
        self._uid: int | None = None
        self._common = xmlrpc_client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self._models = xmlrpc_client.ServerProxy(f"{self.url}/xmlrpc/2/object")

    def connect(self) -> bool:
        """Authenticate with Odoo and cache the UID without logging credentials."""
        try:
            authenticated_uid = self._common.authenticate(self.db, self.username, self.password, {})
            if (
                not isinstance(authenticated_uid, int)
                or isinstance(authenticated_uid, bool)
                or authenticated_uid <= 0
            ):
                logger.warning("odoo_auth_failed", reason="invalid_uid")
                return False
            self._uid = authenticated_uid
            logger.info("odoo_connected", uid=self._uid)
            return True
        except Exception as exc:  # noqa: BLE001 - provider boundary
            logger.warning("odoo_connect_failed", error_type=type(exc).__name__)
            return False

    def test_connection(self) -> bool:
        """Verify the authenticated Odoo session remains usable."""
        if not self._uid:
            return self.connect()
        try:
            result = self._execute("res.users", "search", [[["id", "=", self._uid]]])
            return bool(result)
        except Exception as exc:  # noqa: BLE001 - provider boundary
            logger.warning("odoo_connection_test_failed", error_type=type(exc).__name__)
            return False

    def normalize_record_id(self, record_id: object) -> str:
        """Normalize Odoo's numeric ID as an opaque canonical string."""
        normalized = super().normalize_record_id(record_id)
        try:
            numeric_id = int(normalized)
        except (TypeError, ValueError) as exc:
            raise CRMReadError(
                CRMErrorCode.INVALID_IDENTIFIER,
                "Odoo record id must be an integer-like value",
            ) from exc
        if numeric_id <= 0:
            raise CRMReadError(CRMErrorCode.INVALID_IDENTIFIER, "Odoo record id must be positive")
        return str(numeric_id)

    def normalize_record(
        self,
        tenant_id: str,
        connector_id: str,
        object_type: str,
        record: dict[str, Any],
    ) -> CRMRecord:
        """Normalize an Odoo read result while retaining provider properties."""
        record_id = self.normalize_record_id(record.get("id"))
        properties = dict(record)
        return CRMRecord(
            record_ref=CRMRecordRef(
                tenant_id=tenant_id,
                connector_id=connector_id,
                crm_type=self.credentials.crm_type,
                object_type=object_type,
                record_id=record_id,
            ),
            properties=properties,
            version_or_etag=None,
        )

    def get_record(self, object_type: str, record_id: str) -> dict[str, Any] | None:
        """Fetch a record; return None only when Odoo returns an empty read result."""
        normalized_id = self.normalize_record_id(record_id)
        try:
            results = self._execute(object_type, "read", [[int(normalized_id)]])
        except Exception as exc:  # noqa: BLE001 - mapped immediately below
            raise self._read_error(exc, "Odoo record read failed") from exc
        if not isinstance(results, list):
            raise CRMReadError(CRMErrorCode.MALFORMED_RESPONSE, "Odoo read result is not a list")
        if not results:
            return None
        if not isinstance(results[0], dict):
            raise CRMReadError(CRMErrorCode.MALFORMED_RESPONSE, "Odoo record is not an object")
        return dict(results[0])

    def query_records(
        self,
        object_type: str,
        filters: dict[str, Any],
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return all matching Odoo records or raise a typed remote read failure."""
        domain = [[key, "=", value] for key, value in filters.items()]
        try:
            ids = self._execute(object_type, "search", [domain])
            if not isinstance(ids, list):
                raise CRMReadError(
                    CRMErrorCode.MALFORMED_RESPONSE, "Odoo search result is not a list"
                )
            if not ids:
                return []
            kwargs: dict[str, Any] = {"fields": list(fields)} if fields else {}
            records = self._execute(object_type, "read", [ids], **kwargs)
        except CRMReadError:
            raise
        except Exception as exc:  # noqa: BLE001 - mapped immediately below
            raise self._read_error(exc, "Odoo record query failed") from exc
        if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
            raise CRMReadError(CRMErrorCode.MALFORMED_RESPONSE, "Odoo query records are malformed")
        return [dict(record) for record in records]

    def create_record(self, object_type: str, data: dict[str, Any]) -> WriteResult:
        """Refuse create because R1 only permits trusted update-by-id operations."""
        return self._unsafe_operation_result("create")

    def update_record(
        self,
        object_type: str,
        record_id: str,
        data: dict[str, Any],
        *,
        permit: CRMWritePermit | None = None,
    ) -> WriteResult:
        """Perform a permit-bound update and validate Odoo's exact boolean acknowledgement."""
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
        try:
            acknowledged = self._execute(object_type, "write", [[int(normalized_id)], dict(data)])
        except (TimeoutError, OSError, xmlrpc_client.ProtocolError) as exc:
            logger.warning("odoo_update_indeterminate", error_type=type(exc).__name__)
            return WriteResult(
                success=False,
                record_id=normalized_id,
                error=CRMErrorCode.TIMEOUT.value,
                status=CRMWriteState.INDETERMINATE,
                reconciliation_state="required",
            )
        except xmlrpc_client.Fault as exc:
            logger.warning("odoo_update_rejected", fault_code=exc.faultCode)
            return WriteResult(
                success=False,
                record_id=normalized_id,
                error=CRMErrorCode.REMOTE.value,
                status=CRMWriteState.REJECTED,
            )
        except Exception as exc:  # noqa: BLE001 - provider boundary
            logger.warning("odoo_update_indeterminate", error_type=type(exc).__name__)
            return WriteResult(
                success=False,
                record_id=normalized_id,
                error=CRMErrorCode.REMOTE.value,
                status=CRMWriteState.INDETERMINATE,
                reconciliation_state="required",
            )

        if acknowledged is True:
            return WriteResult(
                success=True,
                record_id=normalized_id,
                fields_written=list(data.keys()),
                status=CRMWriteState.CONFIRMED,
                provider_ack="odoo.write:true",
                reconciliation_state="not_verified",
            )
        if acknowledged is False:
            return WriteResult(
                success=False,
                record_id=normalized_id,
                error=CRMErrorCode.REMOTE.value,
                status=CRMWriteState.REJECTED,
                provider_ack="odoo.write:false",
            )
        return WriteResult(
            success=False,
            record_id=normalized_id,
            error=CRMErrorCode.MALFORMED_RESPONSE.value,
            status=CRMWriteState.REJECTED,
        )

    def upsert_record(
        self,
        object_type: str,
        external_id_field: str,
        external_id_value: str,
        data: dict[str, Any],
    ) -> WriteResult:
        """Refuse unsafe lookup-then-create upsert until uniqueness is independently proven."""
        return self._unsafe_operation_result("upsert")

    def get_field_metadata(self, object_type: str) -> dict[str, Any]:
        """Return Odoo field metadata when the provider responds with the documented shape."""
        try:
            fields = self._execute(
                object_type,
                "fields_get",
                [],
                attributes=["string", "type", "required"],
            )
        except Exception as exc:  # noqa: BLE001 - metadata is advisory
            logger.warning("odoo_field_metadata_failed", error_type=type(exc).__name__)
            return {}
        if not isinstance(fields, dict):
            return {}
        return {
            "name": object_type,
            "fields": [
                {"name": name, **definition}
                for name, definition in fields.items()
                if isinstance(name, str) and isinstance(definition, dict)
            ],
        }

    async def async_connect(self) -> bool:
        """Run connect without blocking an async caller."""
        return await asyncio.to_thread(self.connect)

    async def async_update_record(
        self,
        object_type: str,
        record_id: str,
        data: dict[str, Any],
        *,
        permit: CRMWritePermit | None = None,
    ) -> WriteResult:
        """Run the permit-bound update without blocking an async caller."""
        return await asyncio.to_thread(
            self.update_record,
            object_type,
            record_id,
            data,
            permit=permit,
        )

    def _read_error(self, exc: Exception, message: str) -> CRMReadError:
        """Classify Odoo XML-RPC read failures without leaking request data."""
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return CRMReadError(CRMErrorCode.TIMEOUT, message, retriable=True)
        if isinstance(exc, (OSError, xmlrpc_client.ProtocolError)):
            return CRMReadError(CRMErrorCode.TRANSPORT, message, retriable=True)
        if isinstance(exc, xmlrpc_client.Fault):
            return CRMReadError(CRMErrorCode.REMOTE, message)
        return CRMReadError(CRMErrorCode.REMOTE, message)

    def _execute(
        self,
        model: str,
        method: str,
        args: list[Any],
        **kwargs: Any,
    ) -> Any:
        """Execute an Odoo XML-RPC call after authentication."""
        if not self._uid:
            raise RuntimeError("Not authenticated — call connect() first")
        return self._models.execute_kw(
            self.db,
            self._uid,
            self.password,
            model,
            method,
            args,
            kwargs,
        )
