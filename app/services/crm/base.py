"""
CRM provider-neutral contracts.

This module owns the internal CRM identity, lookup, acknowledgement and write
receipt semantics. It deliberately does not own Gate transport, tenant
resolution, credential resolution, approval authority, or persistent journal
storage. Callers must supply those trusted concerns before requesting an
external mutation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CRMType(StrEnum):
    """Supported CRM platforms."""

    ODOO = "odoo"
    SALESFORCE = "salesforce"
    HUBSPOT = "hubspot"


class CRMErrorCode(StrEnum):
    """Provider-neutral remote failure classifications."""

    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    CONFLICT = "conflict"
    INVALID_IDENTIFIER = "invalid_identifier"
    MALFORMED_RESPONSE = "malformed_response"
    PAGINATION_INCOMPLETE = "pagination_incomplete"
    RATE_LIMIT = "rate_limit"
    REMOTE = "remote"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    WRITE_PERMIT_REQUIRED = "write_permit_required"


class CRMLookupStatus(StrEnum):
    """The complete result of a zero/one/many lookup."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    ERROR = "error"


class CRMWriteState(StrEnum):
    """Internal CRM operation lifecycle states.

    ``CONFIRMED`` is a documented provider acknowledgement. It is not a claim
    that each field was independently read back. ``VERIFIED_POSTWRITE`` is
    reserved for a separately authenticated, version-aware postcondition read.
    """

    DISABLED = "disabled"
    PENDING_REVIEW = "pending_review"
    PREPARED = "prepared"
    CLAIMED = "claimed"
    DISPATCHED = "dispatched"
    CONFIRMED = "confirmed"
    VERIFIED_POSTWRITE = "verified_postwrite"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"


class CRMOperation(StrEnum):
    """Supported semantic CRM operations."""

    UPDATE_BY_ID = "update_by_id"
    UPSERT_BY_UNIQUE_KEY = "upsert_by_unique_key"
    CREATE = "create"


class CRMReadError(RuntimeError):
    """A failed provider read that must not be interpreted as no match."""

    def __init__(
        self,
        code: CRMErrorCode,
        message: str,
        *,
        retriable: bool = False,
        details_ref: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retriable = retriable
        self.details_ref = details_ref


class CRMOperationConflictError(RuntimeError):
    """A durable operation key is bound to different semantic content."""


class CRMJournalError(RuntimeError):
    """The required CRM write journal could not durably record a lifecycle step."""


_PERMIT_ISSUER = object()


@dataclass(frozen=True)
class CRMCredentials:
    """Credential container supplied only by a trusted connector owner."""

    crm_type: CRMType
    credentials: dict[str, str]
    provider_account_id: str = ""


@dataclass(frozen=True)
class CRMRecordRef:
    """Stable, connector-scoped reference to an existing CRM record."""

    tenant_id: str
    connector_id: str
    crm_type: CRMType
    object_type: str
    record_id: str
    provider_account_id: str = ""

    def __post_init__(self) -> None:
        for name, value in (
            ("tenant_id", self.tenant_id),
            ("connector_id", self.connector_id),
            ("object_type", self.object_type),
            ("record_id", self.record_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"CRMRecordRef.{name} must be a non-empty string")


@dataclass(frozen=True)
class CRMUniqueKey:
    """Future explicit unique-key upsert identity; disabled for the R1 path."""

    object_type: str
    configured_field: str
    value: str
    uniqueness_basis: str


@dataclass(frozen=True)
class CRMRecord:
    """Adapter-normalized CRM record returned by a complete provider read."""

    record_ref: CRMRecordRef
    properties: dict[str, Any]
    version_or_etag: str | None = None


@dataclass(frozen=True)
class CRMLookupResult:
    """Explicit zero/one/many lookup result; errors are raised as CRMReadError."""

    status: CRMLookupStatus
    record: CRMRecord | None = None
    ambiguous_count: int = 0
    diagnostic: str | None = None


@dataclass(frozen=True)
class CRMWriteIntent:
    """Durably journaled semantic intent before an external CRM mutation."""

    operation_id: str
    action: CRMOperation
    record_ref: CRMRecordRef
    canonical_field_values: dict[str, Any]
    confidence_evidence: dict[str, float]
    approval_ref: str
    payload_hash: str
    correlation_id: str
    expected_version: str | None = None


@dataclass(frozen=True)
class CRMWritePermit:
    """One claimed operation permit passed from the orchestrator to an adapter.

    The permit is internal. Its presence proves that a trusted caller completed
    policy and durable-journal steps; it is not a substitute for those checks.
    """

    operation_id: str
    record_ref: CRMRecordRef
    payload_hash: str
    payload_digest: str
    _issuer: object | None = field(default=None, repr=False, compare=False)


def _issue_write_permit(
    operation_id: str,
    record_ref: CRMRecordRef,
    payload_hash: str,
    payload: dict[str, Any],
) -> CRMWritePermit:
    """Issue the internal capability after the journal has claimed an operation.

    This function is deliberately private: application code must obtain a
    permit only through ``WriteBackOrchestrator`` after trusted approval and a
    durable journal claim. The adapter independently checks the payload digest
    before a provider call, so a permit cannot be reused for different fields.
    """
    return CRMWritePermit(
        operation_id=operation_id,
        record_ref=record_ref,
        payload_hash=payload_hash,
        payload_digest=crm_payload_digest(record_ref, payload),
        _issuer=_PERMIT_ISSUER,
    )


def crm_payload_digest(record_ref: CRMRecordRef, payload: dict[str, Any]) -> str:
    """Return a stable digest of the exact provider object, ID, account, and values."""
    import hashlib
    import json

    material = {
        "tenant_id": record_ref.tenant_id,
        "connector_id": record_ref.connector_id,
        "provider_account_id": record_ref.provider_account_id,
        "crm_type": record_ref.crm_type.value,
        "object_type": record_ref.object_type,
        "record_id": record_ref.record_id,
        "fields": payload,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CRMWriteReceipt:
    """Internal result of an attempted CRM operation."""

    status: CRMWriteState
    record_ref: CRMRecordRef | None
    attempted_fields: tuple[str, ...] = ()
    applied_fields: tuple[str, ...] = ()
    skipped_fields: tuple[str, ...] = ()
    error_code: CRMErrorCode | str | None = None
    provider_ack: str | None = None
    operation_id: str | None = None
    correlation_id: str | None = None
    audit_ref: str | None = None
    reconciliation_state: str | None = None

    @property
    def success(self) -> bool:
        """Return the legacy success projection without treating uncertainty as success."""
        return self.status in {CRMWriteState.CONFIRMED, CRMWriteState.VERIFIED_POSTWRITE}

    def to_write_result(self) -> WriteResult:
        """Project this receipt to the preserved caller-facing WriteResult shape."""
        return WriteResult(
            success=self.success,
            record_id=self.record_ref.record_id if self.record_ref else "",
            fields_written=list(self.applied_fields) if self.success else [],
            error=str(self.error_code or ""),
            status=self.status,
            provider_ack=self.provider_ack,
            reconciliation_state=self.reconciliation_state,
        )


@dataclass
class WriteResult:
    """Backward-compatible projection of a single CRM write acknowledgement.

    ``success`` is true only for a documented provider acknowledgement or a
    separately verified post-write state. In particular, it is always false for
    ``INDETERMINATE`` so legacy callers cannot mistake an uncertain send for a
    completed mutation.
    """

    success: bool
    record_id: str = ""
    fields_written: list[str] = field(default_factory=list)
    error: str = ""
    status: CRMWriteState | None = None
    provider_ack: str | None = None
    reconciliation_state: str | None = None

    def __post_init__(self) -> None:
        if self.status is None:
            self.status = CRMWriteState.CONFIRMED if self.success else CRMWriteState.REJECTED
        self.success = self.status in {CRMWriteState.CONFIRMED, CRMWriteState.VERIFIED_POSTWRITE}
        if not self.success:
            self.fields_written = []


class CRMClientBase(ABC):
    """Abstract provider interface guarded by a claimed CRMWritePermit.

    ``query_records`` preserves its established successful ``list[dict]`` API.
    Remote read failures must raise ``CRMReadError`` rather than returning an
    empty collection. Mutating methods only accept the R1 update-by-id path
    after the provider-neutral orchestrator supplies a bound permit.
    """

    def __init__(self, credentials: CRMCredentials) -> None:
        self.credentials = credentials

    @abstractmethod
    def connect(self) -> bool:
        """Establish a provider connection. Returns true only when authenticated."""
        raise NotImplementedError

    @abstractmethod
    def test_connection(self) -> bool:
        """Verify the provider connection is still alive."""
        raise NotImplementedError

    @abstractmethod
    def get_record(self, object_type: str, record_id: str) -> dict[str, Any] | None:
        """Fetch one record or return None only for verified provider absence."""
        raise NotImplementedError

    @abstractmethod
    def query_records(
        self,
        object_type: str,
        filters: dict[str, Any],
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return complete successful query results or raise CRMReadError."""
        raise NotImplementedError

    @abstractmethod
    def normalize_record(
        self,
        tenant_id: str,
        connector_id: str,
        object_type: str,
        record: dict[str, Any],
    ) -> CRMRecord:
        """Normalize provider output to a connector-scoped CRMRecord."""
        raise NotImplementedError

    def normalize_record_id(self, record_id: object) -> str:
        """Normalize an opaque provider identifier without guessing record fields."""
        if record_id is None:
            raise CRMReadError(CRMErrorCode.MALFORMED_RESPONSE, "provider record id is missing")
        normalized = str(record_id).strip()
        if not normalized:
            raise CRMReadError(CRMErrorCode.MALFORMED_RESPONSE, "provider record id is empty")
        return normalized

    def lookup_unique(
        self,
        tenant_id: str,
        connector_id: str,
        object_type: str,
        filters: dict[str, Any],
        fields: list[str] | None = None,
    ) -> CRMLookupResult:
        """Perform a complete adapter-normalized zero/one/many lookup.

        Provider errors deliberately propagate as ``CRMReadError``. A caller
        that needs a record update must reject an ambiguous result rather than
        selecting its first row.
        """
        rows = self.query_records(object_type, filters, fields)
        normalized_by_id: dict[str, CRMRecord] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise CRMReadError(
                    CRMErrorCode.MALFORMED_RESPONSE,
                    "provider query result contains a non-object record",
                )
            normalized = self.normalize_record(tenant_id, connector_id, object_type, row)
            normalized_by_id[normalized.record_ref.record_id] = normalized

        normalized_records = list(normalized_by_id.values())
        if not normalized_records:
            return CRMLookupResult(status=CRMLookupStatus.NOT_FOUND)
        if len(normalized_records) == 1:
            return CRMLookupResult(status=CRMLookupStatus.FOUND, record=normalized_records[0])
        return CRMLookupResult(
            status=CRMLookupStatus.AMBIGUOUS,
            ambiguous_count=len(normalized_records),
            diagnostic="multiple distinct provider record identifiers matched the lookup",
        )

    @abstractmethod
    def create_record(self, object_type: str, data: dict[str, Any]) -> WriteResult:
        """Create is intentionally disabled in the initial safe writeback release."""
        raise NotImplementedError

    @abstractmethod
    def update_record(
        self,
        object_type: str,
        record_id: str,
        data: dict[str, Any],
        *,
        permit: CRMWritePermit | None = None,
    ) -> WriteResult:
        """Update one existing record only when a matching permit is supplied."""
        raise NotImplementedError

    @abstractmethod
    def upsert_record(
        self,
        object_type: str,
        external_id_field: str,
        external_id_value: str,
        data: dict[str, Any],
    ) -> WriteResult:
        """Upsert is intentionally disabled until uniqueness semantics are proven."""
        raise NotImplementedError

    def bulk_create(self, object_type: str, records: list[dict[str, Any]]) -> list[WriteResult]:
        """Refuse bulk creation so a batch cannot bypass the safe update path."""
        return [self._unsafe_operation_result("bulk_create") for _ in records]

    def bulk_update(self, object_type: str, records: list[dict[str, Any]]) -> list[WriteResult]:
        """Refuse bulk update so per-record permits and receipts cannot be bypassed."""
        return [self._unsafe_operation_result("bulk_update") for _ in records]

    @abstractmethod
    def get_field_metadata(self, object_type: str) -> dict[str, Any]:
        """Return field schema metadata for a CRM object."""
        raise NotImplementedError

    def validate_update_permit(
        self,
        object_type: str,
        record_id: str,
        data: dict[str, Any],
        permit: CRMWritePermit | None,
    ) -> WriteResult | None:
        """Reject a direct adapter mutation lacking its claimed operation permit."""
        if permit is None or permit._issuer is not _PERMIT_ISSUER:
            return WriteResult(
                success=False,
                record_id=str(record_id),
                error=CRMErrorCode.WRITE_PERMIT_REQUIRED.value,
                status=CRMWriteState.DISABLED,
            )
        ref = permit.record_ref
        if (
            ref.crm_type != self.credentials.crm_type
            or ref.object_type != object_type
            or ref.record_id != str(record_id).strip()
            or (
                bool(self.credentials.provider_account_id)
                and ref.provider_account_id != self.credentials.provider_account_id
            )
        ):
            return WriteResult(
                success=False,
                record_id=str(record_id),
                error=CRMErrorCode.CONFLICT.value,
                status=CRMWriteState.REJECTED,
            )
        if permit.payload_digest != crm_payload_digest(ref, data):
            return WriteResult(
                success=False,
                record_id=str(record_id),
                error=CRMErrorCode.CONFLICT.value,
                status=CRMWriteState.REJECTED,
            )
        return None

    def _unsafe_operation_result(self, operation: str) -> WriteResult:
        """Build the shared no-I/O response for operations excluded from R1."""
        return WriteResult(
            success=False,
            error=f"{operation}_disabled",
            status=CRMWriteState.DISABLED,
        )
