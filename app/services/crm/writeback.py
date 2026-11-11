"""Provider-neutral CRM writeback orchestration.

The orchestrator accepts only a connector binding and approval decision supplied
by an authorized owner. It does not resolve tenants, credentials, mappings, or
approval from a request payload, and it has no implicit create or upsert path.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import structlog

from .base import (
    CRMClientBase,
    CRMCredentials,
    CRMErrorCode,
    CRMOperation,
    CRMOperationConflictError,
    CRMRecordRef,
    CRMWriteIntent,
    CRMWriteReceipt,
    CRMWriteState,
    WriteResult,
    _issue_write_permit,
    crm_provider_key,
    resolve_crm_client,
)
from .field_mapper import FieldMapper

logger = structlog.get_logger("crm_writeback")


class CRMActivationLevel(StrEnum):
    """Per-connector write activation levels supplied by the trusted owner."""

    DISABLED = "disabled"
    LOCAL_TEST = "local_test"
    GATE_SANDBOX = "gate_sandbox"
    PRODUCTION_ENABLED = "production_enabled"


@dataclass(frozen=True)
class TrustedCRMConnector:
    """Resolved per-tenant connector binding; never construct it from request JSON.

    ``crm_type`` is the provider key the adapter registered under (see
    ``register_crm_client``); the orchestrator resolves it through the registry
    and never compares it against a provider name of its own.
    """

    tenant_id: str
    connector_id: str
    provider_account_id: str
    crm_type: str
    credentials: dict[str, str]
    mapping_path: str
    allowed_objects: frozenset[str]
    allowed_fields: dict[str, frozenset[str]]
    activation: CRMActivationLevel = CRMActivationLevel.DISABLED

    def __post_init__(self) -> None:
        for name, value in (
            ("tenant_id", self.tenant_id),
            ("connector_id", self.connector_id),
            ("provider_account_id", self.provider_account_id),
            ("mapping_path", self.mapping_path),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"TrustedCRMConnector.{name} must be a non-empty string")
        try:
            crm_provider_key(self.crm_type)
        except ValueError as exc:
            raise ValueError(
                "TrustedCRMConnector.crm_type must be a non-empty provider key"
            ) from exc
        if not self.allowed_objects:
            raise ValueError("TrustedCRMConnector.allowed_objects must not be empty")


@dataclass(frozen=True)
class TrustedWritebackApproval:
    """A record-and-field-hash-bound decision obtained from an authoritative owner."""

    approval_ref: str
    actor_id: str
    tenant_id: str
    connector_id: str
    provider_account_id: str
    object_type: str
    record_id: str
    operation_id: str
    payload_hash: str
    expires_at: datetime
    policy_version: str
    revoked: bool = False

    def validate(self, intent: CRMWriteIntent) -> None:
        """Verify every immutable binding before a journaled provider mutation."""
        ref = intent.record_ref
        if self.revoked:
            raise PermissionError("writeback approval is revoked")
        if self.expires_at.tzinfo is None or self.expires_at <= datetime.now(UTC):
            raise PermissionError("writeback approval is expired")
        bindings_match = (
            self.tenant_id == ref.tenant_id
            and self.connector_id == ref.connector_id
            and self.provider_account_id == ref.provider_account_id
            and self.object_type == ref.object_type
            and self.record_id == ref.record_id
            and self.operation_id == intent.operation_id
            and self.payload_hash == intent.payload_hash
            and self.approval_ref == intent.approval_ref
        )
        if not bindings_match:
            raise PermissionError(
                "writeback approval does not match the operation identity or payload"
            )


@dataclass(frozen=True)
class CRMJournalClaim:
    """Atomic journal claim result; a previous receipt blocks another dispatch."""

    may_dispatch: bool
    existing_receipt: CRMWriteReceipt | None = None
    audit_ref: str | None = None


class CRMOperationJournal(Protocol):
    """Durable single-writer journal contract injected by the persistence owner."""

    def prepare(self, intent: CRMWriteIntent) -> None:
        """Persist the prepared intent before any provider mutation is attempted."""

    def claim(self, intent: CRMWriteIntent) -> CRMJournalClaim:
        """Atomically claim the exact operation or return its prior durable receipt."""

    def mark_dispatched(self, intent: CRMWriteIntent) -> None:
        """Durably mark the operation as possibly sent immediately before provider I/O."""

    def finalize(self, receipt: CRMWriteReceipt) -> CRMWriteReceipt:
        """Durably persist the terminal acknowledgement or indeterminate receipt."""


ClientFactory = Callable[[CRMCredentials], CRMClientBase]


class WriteBackOrchestrator:
    """Execute one trusted, durable, provider-neutral ``UPDATE_BY_ID`` operation."""

    def __init__(
        self,
        connector: TrustedCRMConnector,
        journal: CRMOperationJournal,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.connector = connector
        self.journal = journal
        self.mapper = FieldMapper(Path(connector.mapping_path), expected_crm=connector.crm_type)
        self._client_factory = client_factory

    def write_update_by_id(
        self,
        *,
        domain: str,
        record_id: str,
        canonical: dict[str, Any],
        approval: TrustedWritebackApproval,
        operation_id: str,
        correlation_id: str,
        confidence_evidence: dict[str, float] | None = None,
        expected_version: str | None = None,
    ) -> CRMWriteReceipt:
        """Journal and dispatch a single approved update-by-ID without implicit lookup/create.

        The caller must already have obtained the trusted tenant connector and
        approval decision. Any unavailable, disabled, malformed, conflict, or
        journal condition returns a non-success receipt before provider mutation.
        """
        # Activation is the connector owner's decision per connector. The
        # orchestrator never gates on which provider the connector names.
        if self.connector.activation == CRMActivationLevel.DISABLED:
            return self._rejected_receipt(
                None,
                CRMWriteState.DISABLED,
                "connector_writeback_disabled",
                operation_id,
                correlation_id,
            )
        if not isinstance(canonical, dict):
            return self._rejected_receipt(
                None,
                CRMWriteState.REJECTED,
                "canonical_payload_invalid",
                operation_id,
                correlation_id,
            )
        if not operation_id.strip() or not correlation_id.strip():
            return self._rejected_receipt(
                None,
                CRMWriteState.REJECTED,
                "operation_or_correlation_id_missing",
                operation_id,
                correlation_id,
            )

        try:
            crm_object = self.mapper.object_for_domain(domain)
            if crm_object not in self.connector.allowed_objects:
                return self._rejected_receipt(
                    None,
                    CRMWriteState.REJECTED,
                    "object_not_allowed_by_trusted_connector",
                    operation_id,
                    correlation_id,
                )
            allowed_fields = self.connector.allowed_fields.get(crm_object, frozenset())
            _, payload, skipped = self.mapper.to_approved_crm(domain, canonical, allowed_fields)
            client = self._new_client()
            normalized_record_id = client.normalize_record_id(record_id)
        except LookupError as exc:
            logger.warning("writeback_preflight_rejected", reason=type(exc).__name__)
            return self._rejected_receipt(
                None,
                CRMWriteState.REJECTED,
                "provider_not_registered",
                operation_id,
                correlation_id,
            )
        except ValueError as exc:
            logger.warning("writeback_preflight_rejected", reason=type(exc).__name__)
            return self._rejected_receipt(
                None,
                CRMWriteState.REJECTED,
                "trusted_mapping_or_identity_invalid",
                operation_id,
                correlation_id,
            )
        except Exception as exc:  # noqa: BLE001 - typed provider identity errors are mapped safely
            logger.warning("writeback_preflight_rejected", reason=type(exc).__name__)
            return self._rejected_receipt(
                None,
                CRMWriteState.REJECTED,
                "provider_identity_invalid",
                operation_id,
                correlation_id,
            )

        record_ref = CRMRecordRef(
            tenant_id=self.connector.tenant_id,
            connector_id=self.connector.connector_id,
            crm_type=self.connector.crm_type,
            object_type=crm_object,
            record_id=normalized_record_id,
            provider_account_id=self.connector.provider_account_id,
        )
        if not payload:
            return CRMWriteReceipt(
                status=CRMWriteState.REJECTED,
                record_ref=record_ref,
                skipped_fields=tuple(sorted(skipped)),
                error_code="empty_payload_after_trusted_filter",
                operation_id=operation_id,
                correlation_id=correlation_id,
            )
        if expected_version is not None:
            return self._rejected_receipt(
                record_ref,
                CRMWriteState.REJECTED,
                "expected_version_unsupported",
                operation_id,
                correlation_id,
                skipped,
            )

        payload_hash = _payload_hash(record_ref, payload, expected_version)
        intent = CRMWriteIntent(
            operation_id=operation_id,
            action=CRMOperation.UPDATE_BY_ID,
            record_ref=record_ref,
            canonical_field_values=dict(canonical),
            confidence_evidence=dict(confidence_evidence or {}),
            approval_ref=approval.approval_ref,
            expected_version=expected_version,
            payload_hash=payload_hash,
            correlation_id=correlation_id,
        )
        try:
            approval.validate(intent)
        except PermissionError:
            return self._rejected_receipt(
                record_ref,
                CRMWriteState.REJECTED,
                "trusted_approval_mismatch",
                operation_id,
                correlation_id,
                skipped,
            )

        try:
            self.journal.prepare(intent)
            claim = self.journal.claim(intent)
        except CRMOperationConflictError:
            return self._rejected_receipt(
                record_ref,
                CRMWriteState.REJECTED,
                CRMErrorCode.CONFLICT.value,
                operation_id,
                correlation_id,
                skipped,
            )
        except Exception as exc:  # noqa: BLE001 - no provider I/O after journal failure
            logger.warning("writeback_journal_prepare_failed", error_type=type(exc).__name__)
            return self._rejected_receipt(
                record_ref,
                CRMWriteState.REJECTED,
                "journal_unavailable_before_dispatch",
                operation_id,
                correlation_id,
                skipped,
            )

        if claim.existing_receipt is not None:
            return claim.existing_receipt
        if not claim.may_dispatch:
            return self._rejected_receipt(
                record_ref,
                CRMWriteState.REJECTED,
                "journal_claim_denied",
                operation_id,
                correlation_id,
                skipped,
                audit_ref=claim.audit_ref,
            )

        try:
            if not client.connect():
                receipt = self._rejected_receipt(
                    record_ref,
                    CRMWriteState.REJECTED,
                    "provider_connection_rejected",
                    operation_id,
                    correlation_id,
                    skipped,
                    audit_ref=claim.audit_ref,
                )
                return self.journal.finalize(receipt)
            approval.validate(intent)
            self.journal.mark_dispatched(intent)
        except PermissionError:
            logger.warning("writeback_approval_expired_before_dispatch")
            return self._rejected_receipt(
                record_ref,
                CRMWriteState.REJECTED,
                "trusted_approval_expired",
                operation_id,
                correlation_id,
                skipped,
                audit_ref=claim.audit_ref,
            )
        except Exception as exc:  # noqa: BLE001 - no mutation when dispatch state is not durable
            logger.warning("writeback_dispatch_precondition_failed", error_type=type(exc).__name__)
            return self._rejected_receipt(
                record_ref,
                CRMWriteState.REJECTED,
                "journal_unavailable_before_dispatch",
                operation_id,
                correlation_id,
                skipped,
                audit_ref=claim.audit_ref,
            )

        try:
            approval.validate(intent)
        except PermissionError:
            logger.warning("writeback_approval_expired_before_dispatch")
            return self._rejected_receipt(
                record_ref,
                CRMWriteState.REJECTED,
                "trusted_approval_expired",
                operation_id,
                correlation_id,
                skipped,
                audit_ref=claim.audit_ref,
            )
        permit = _issue_write_permit(operation_id, record_ref, payload_hash, payload)
        try:
            result = client.update_record(crm_object, normalized_record_id, payload, permit=permit)
        except Exception as exc:  # noqa: BLE001 - provider call may have reached the remote CRM
            logger.warning("writeback_adapter_indeterminate", error_type=type(exc).__name__)
            result = WriteResult(
                success=False,
                record_id=normalized_record_id,
                error="adapter_exception_after_dispatch",
                status=CRMWriteState.INDETERMINATE,
                reconciliation_state="required",
            )
        receipt = CRMWriteReceipt(
            status=result.status or CRMWriteState.REJECTED,
            record_ref=record_ref,
            attempted_fields=tuple(sorted(payload)),
            applied_fields=tuple(sorted(result.fields_written)) if result.success else (),
            skipped_fields=tuple(sorted(skipped)),
            error_code=result.error or None,
            provider_ack=result.provider_ack,
            operation_id=operation_id,
            correlation_id=correlation_id,
            audit_ref=claim.audit_ref,
            reconciliation_state=result.reconciliation_state,
        )
        try:
            return self.journal.finalize(receipt)
        except Exception as exc:  # noqa: BLE001 - a sent call cannot be reported as durably completed
            logger.warning("writeback_journal_finalize_failed", error_type=type(exc).__name__)
            return CRMWriteReceipt(
                status=CRMWriteState.INDETERMINATE,
                record_ref=record_ref,
                attempted_fields=tuple(sorted(payload)),
                skipped_fields=tuple(sorted(skipped)),
                error_code="journal_finalize_failed",
                provider_ack=result.provider_ack,
                operation_id=operation_id,
                correlation_id=correlation_id,
                audit_ref=claim.audit_ref,
                reconciliation_state="required",
            )

    def write_back(self, domain: str, canonical: dict[str, Any]) -> WriteResult:
        """Preserve the legacy call shape as a safe disabled projection.

        The legacy call lacks an existing record identity, connector binding,
        approval decision, operation identity, and durable journal. It must not
        construct a provider client or make an external request.
        """
        logger.warning("writeback_legacy_call_refused", domain=domain)
        return WriteResult(
            success=False,
            error="trusted_update_by_id_context_required",
            status=CRMWriteState.DISABLED,
        )

    async def async_write_back(self, domain: str, canonical: dict[str, Any]) -> WriteResult:
        """Return the safe legacy refusal without offloading a provider operation."""
        return self.write_back(domain, canonical)

    def _new_client(self) -> CRMClientBase:
        """Instantiate the registered provider only after trusted preflight.

        Raises ``LookupError`` when no adapter is registered for the connector's
        provider key; the caller reports that as ``provider_not_registered``.
        """
        credentials = CRMCredentials(
            crm_type=self.connector.crm_type,
            credentials=self.connector.credentials,
            provider_account_id=self.connector.provider_account_id,
        )
        if self._client_factory is not None:
            return self._client_factory(credentials)
        return resolve_crm_client(self.connector.crm_type)(credentials)

    def _rejected_receipt(
        self,
        record_ref: CRMRecordRef | None,
        state: CRMWriteState,
        error_code: str,
        operation_id: str,
        correlation_id: str,
        skipped: dict[str, str] | None = None,
        *,
        audit_ref: str | None = None,
    ) -> CRMWriteReceipt:
        """Build a consistent no-I/O rejection receipt without including payload values."""
        return CRMWriteReceipt(
            status=state,
            record_ref=record_ref,
            skipped_fields=tuple(sorted(skipped or {})),
            error_code=error_code,
            operation_id=operation_id or None,
            correlation_id=correlation_id or None,
            audit_ref=audit_ref,
        )


def _payload_hash(
    record_ref: CRMRecordRef,
    payload: dict[str, Any],
    expected_version: str | None,
) -> str:
    """Create the exact stable operation digest bound by approval and journal ownership."""
    material = {
        "tenant_id": record_ref.tenant_id,
        "connector_id": record_ref.connector_id,
        "provider_account_id": record_ref.provider_account_id,
        "crm_type": crm_provider_key(record_ref.crm_type),
        "object_type": record_ref.object_type,
        "record_id": record_ref.record_id,
        "operation": CRMOperation.UPDATE_BY_ID.value,
        "fields": payload,
        "expected_version": expected_version,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
