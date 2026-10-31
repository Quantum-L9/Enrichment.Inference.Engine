"""Provider-neutral trusted writeback orchestration tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.agents.mcp_server import MCPServer
from app.engines.handlers import handle_writeback
from app.services.crm.base import (
    CRMClientBase,
    CRMCredentials,
    CRMOperationConflictError,
    CRMRecord,
    CRMRecordRef,
    CRMType,
    CRMWritePermit,
    CRMWriteReceipt,
    CRMWriteState,
    WriteResult,
)
from app.services.crm.writeback import (
    CRMActivationLevel,
    CRMJournalClaim,
    TrustedCRMConnector,
    TrustedWritebackApproval,
    WriteBackOrchestrator,
    _payload_hash,
)


class FakeProvider(CRMClientBase):
    """No-network adapter that records whether a permit-bound mutation was requested."""

    def __init__(self, credentials: CRMCredentials) -> None:
        super().__init__(credentials)
        self.connected = False
        self.update_calls: list[tuple[str, str, dict[str, Any], CRMWritePermit | None]] = []
        self.raise_after_dispatch = False

    def connect(self) -> bool:
        self.connected = True
        return True

    def test_connection(self) -> bool:
        return self.connected

    def get_record(self, object_type: str, record_id: str) -> dict[str, Any] | None:
        return None

    def query_records(
        self,
        object_type: str,
        filters: dict[str, Any],
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return []

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
        if denied is not None:
            return denied
        self.update_calls.append((object_type, record_id, dict(data), permit))
        if self.raise_after_dispatch:
            raise RuntimeError("provider transport reset after dispatch")
        return WriteResult(
            success=True,
            record_id=record_id,
            fields_written=list(data),
            status=CRMWriteState.CONFIRMED,
            provider_ack="fake:update:confirmed",
            reconciliation_state="not_verified",
        )

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


class MemoryJournal:
    """Deterministic test double modeling a durable journal's atomic claim behavior."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.payload_hashes: dict[str, str] = {}
        self.receipts: dict[str, CRMWriteReceipt] = {}
        self.fail_prepare = False
        self.fail_finalize = False

    def prepare(self, intent) -> None:
        self.steps.append("prepare")
        if self.fail_prepare:
            raise RuntimeError("store unavailable")
        previous_hash = self.payload_hashes.get(intent.operation_id)
        if previous_hash is not None and previous_hash != intent.payload_hash:
            raise CRMOperationConflictError("different payload")
        self.payload_hashes[intent.operation_id] = intent.payload_hash

    def claim(self, intent) -> CRMJournalClaim:
        self.steps.append("claim")
        if intent.operation_id in self.receipts:
            return CRMJournalClaim(
                may_dispatch=False, existing_receipt=self.receipts[intent.operation_id]
            )
        return CRMJournalClaim(may_dispatch=True, audit_ref=f"audit:{intent.operation_id}")

    def mark_dispatched(self, intent) -> None:
        self.steps.append("dispatched")

    def finalize(self, receipt: CRMWriteReceipt) -> CRMWriteReceipt:
        self.steps.append("finalize")
        if self.fail_finalize:
            raise RuntimeError("store unavailable")
        assert receipt.operation_id is not None
        self.receipts[receipt.operation_id] = receipt
        return receipt


@pytest.fixture
def mapping_file(tmp_path) -> str:
    path = tmp_path / "hubspot.yaml"
    path.write_text(
        """
crm: hubspot
version: "2.0"
objects:
  company:
    hubspot_object: companies
    mappings:
      company_id: hs_object_id
      company_name: name
      company_owner: hubspot_owner_id
      company_industry: industry
      company_location_state: state_id
""".strip(),
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture
def connector(mapping_file: str) -> TrustedCRMConnector:
    return TrustedCRMConnector(
        tenant_id="tenant-a",
        connector_id="hubspot-sandbox-a",
        provider_account_id="portal-a",
        crm_type=CRMType.HUBSPOT,
        credentials={"access_token": "test-token"},
        mapping_path=mapping_file,
        allowed_objects=frozenset({"companies"}),
        allowed_fields={"companies": frozenset({"name", "industry"})},
        activation=CRMActivationLevel.LOCAL_TEST,
    )


def _approval(
    connector: TrustedCRMConnector,
    operation_id: str,
    fields: dict[str, Any],
    *,
    record_id: str = "42",
) -> TrustedWritebackApproval:
    reference = CRMRecordRef(
        tenant_id=connector.tenant_id,
        connector_id=connector.connector_id,
        crm_type=connector.crm_type,
        object_type="companies",
        record_id=record_id,
        provider_account_id=connector.provider_account_id,
    )
    payload_hash = _payload_hash(reference, fields, None)
    return TrustedWritebackApproval(
        approval_ref="approval-1",
        actor_id="operator-1",
        tenant_id=connector.tenant_id,
        connector_id=connector.connector_id,
        provider_account_id=connector.provider_account_id,
        object_type="companies",
        record_id=record_id,
        operation_id=operation_id,
        payload_hash=payload_hash,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        policy_version="policy-v1",
    )


def test_registry_neutral_orchestrator_maps_only_trusted_allowed_fields(
    connector: TrustedCRMConnector,
) -> None:
    journal = MemoryJournal()
    provider = FakeProvider(CRMCredentials(CRMType.HUBSPOT, {}))
    orchestrator = WriteBackOrchestrator(
        connector,
        journal,
        client_factory=lambda credentials: provider,
    )
    fields = {
        "company_id": "42",
        "company_name": "Acme",
        "company_owner": "user-7",
        "company_industry": "Recycling",
    }

    receipt = orchestrator.write_update_by_id(
        domain="company",
        record_id="42",
        canonical=fields,
        approval=_approval(connector, "op-1", {"name": "Acme", "industry": "Recycling"}),
        operation_id="op-1",
        correlation_id="correlation-1",
    )

    assert receipt.status == CRMWriteState.CONFIRMED
    assert receipt.applied_fields == ("industry", "name")
    assert set(receipt.skipped_fields) == {"company_id", "company_owner"}
    assert provider.update_calls[0][0:3] == (
        "companies",
        "42",
        {"name": "Acme", "industry": "Recycling"},
    )
    assert journal.steps == ["prepare", "claim", "dispatched", "finalize"]


def test_empty_post_filter_payload_never_connects_or_updates(
    connector: TrustedCRMConnector,
) -> None:
    journal = MemoryJournal()
    provider = FakeProvider(CRMCredentials(CRMType.HUBSPOT, {}))
    orchestrator = WriteBackOrchestrator(
        connector,
        journal,
        client_factory=lambda credentials: provider,
    )

    receipt = orchestrator.write_update_by_id(
        domain="company",
        record_id="42",
        canonical={"company_id": "42", "company_owner": "user-7"},
        approval=_approval(connector, "op-2", {}),
        operation_id="op-2",
        correlation_id="correlation-2",
    )

    assert receipt.status == CRMWriteState.REJECTED
    assert receipt.error_code == "empty_payload_after_trusted_filter"
    assert provider.connected is False
    assert provider.update_calls == []
    assert journal.steps == []


def test_relationship_target_is_denied_even_when_present_in_trusted_allowlist(
    connector: TrustedCRMConnector,
) -> None:
    journal = MemoryJournal()
    provider = FakeProvider(CRMCredentials(CRMType.HUBSPOT, {}))
    permissive_connector = replace(
        connector,
        allowed_fields={"companies": frozenset({"name", "industry", "state_id"})},
    )
    orchestrator = WriteBackOrchestrator(
        permissive_connector,
        journal,
        client_factory=lambda credentials: provider,
    )

    receipt = orchestrator.write_update_by_id(
        domain="company",
        record_id="42",
        canonical={"company_name": "Acme", "company_location_state": "CA"},
        approval=_approval(permissive_connector, "op-relationship", {"name": "Acme"}),
        operation_id="op-relationship",
        correlation_id="correlation-relationship",
    )

    assert receipt.status == CRMWriteState.CONFIRMED
    assert receipt.skipped_fields == ("company_location_state",)
    assert provider.update_calls[0][2] == {"name": "Acme"}


def test_mismatched_approval_is_refused_before_journal_or_provider_io(
    connector: TrustedCRMConnector,
) -> None:
    journal = MemoryJournal()
    provider = FakeProvider(CRMCredentials(CRMType.HUBSPOT, {}))
    orchestrator = WriteBackOrchestrator(
        connector,
        journal,
        client_factory=lambda credentials: provider,
    )

    receipt = orchestrator.write_update_by_id(
        domain="company",
        record_id="42",
        canonical={"company_name": "Acme"},
        approval=_approval(connector, "op-3", {"name": "different"}),
        operation_id="op-3",
        correlation_id="correlation-3",
    )

    assert receipt.status == CRMWriteState.REJECTED
    assert receipt.error_code == "trusted_approval_mismatch"
    assert journal.steps == []
    assert provider.update_calls == []


def test_provider_account_mismatch_is_refused_before_journal_or_provider_io(
    connector: TrustedCRMConnector,
) -> None:
    journal = MemoryJournal()
    provider = FakeProvider(CRMCredentials(CRMType.HUBSPOT, {}))
    orchestrator = WriteBackOrchestrator(
        connector,
        journal,
        client_factory=lambda credentials: provider,
    )
    approval = replace(
        _approval(connector, "op-account", {"name": "Acme"}),
        provider_account_id="different-portal",
    )

    receipt = orchestrator.write_update_by_id(
        domain="company",
        record_id="42",
        canonical={"company_name": "Acme"},
        approval=approval,
        operation_id="op-account",
        correlation_id="correlation-account",
    )

    assert receipt.status == CRMWriteState.REJECTED
    assert receipt.error_code == "trusted_approval_mismatch"
    assert journal.steps == []
    assert provider.update_calls == []


def test_journal_failure_before_dispatch_means_zero_provider_io(
    connector: TrustedCRMConnector,
) -> None:
    journal = MemoryJournal()
    journal.fail_prepare = True
    provider = FakeProvider(CRMCredentials(CRMType.HUBSPOT, {}))
    orchestrator = WriteBackOrchestrator(
        connector,
        journal,
        client_factory=lambda credentials: provider,
    )
    approved_fields = {"name": "Acme"}

    receipt = orchestrator.write_update_by_id(
        domain="company",
        record_id="42",
        canonical={"company_name": "Acme"},
        approval=_approval(connector, "op-4", approved_fields),
        operation_id="op-4",
        correlation_id="correlation-4",
    )

    assert receipt.error_code == "journal_unavailable_before_dispatch"
    assert provider.connected is False
    assert provider.update_calls == []


def test_same_operation_and_content_replays_durable_receipt_without_second_write(
    connector: TrustedCRMConnector,
) -> None:
    journal = MemoryJournal()
    provider = FakeProvider(CRMCredentials(CRMType.HUBSPOT, {}))
    orchestrator = WriteBackOrchestrator(
        connector,
        journal,
        client_factory=lambda credentials: provider,
    )
    approved_fields = {"name": "Acme"}
    kwargs = {
        "domain": "company",
        "record_id": "42",
        "canonical": {"company_name": "Acme"},
        "approval": _approval(connector, "op-5", approved_fields),
        "operation_id": "op-5",
        "correlation_id": "correlation-5",
    }

    first = orchestrator.write_update_by_id(**kwargs)
    second = orchestrator.write_update_by_id(**kwargs)

    assert first == second
    assert len(provider.update_calls) == 1
    assert journal.steps == ["prepare", "claim", "dispatched", "finalize", "prepare", "claim"]


def test_same_operation_with_changed_content_conflicts_without_second_write(
    connector: TrustedCRMConnector,
) -> None:
    journal = MemoryJournal()
    provider = FakeProvider(CRMCredentials(CRMType.HUBSPOT, {}))
    orchestrator = WriteBackOrchestrator(
        connector,
        journal,
        client_factory=lambda credentials: provider,
    )
    first_fields = {"name": "Acme"}
    first = orchestrator.write_update_by_id(
        domain="company",
        record_id="42",
        canonical={"company_name": "Acme"},
        approval=_approval(connector, "op-6", first_fields),
        operation_id="op-6",
        correlation_id="correlation-6",
    )
    second = orchestrator.write_update_by_id(
        domain="company",
        record_id="42",
        canonical={"company_name": "Changed"},
        approval=_approval(connector, "op-6", {"name": "Changed"}),
        operation_id="op-6",
        correlation_id="correlation-6",
    )

    assert first.status == CRMWriteState.CONFIRMED
    assert second.status == CRMWriteState.REJECTED
    assert second.error_code == "conflict"
    assert len(provider.update_calls) == 1


def test_adapter_exception_after_dispatch_is_durably_indeterminate(
    connector: TrustedCRMConnector,
) -> None:
    journal = MemoryJournal()
    provider = FakeProvider(CRMCredentials(CRMType.HUBSPOT, {}))
    provider.raise_after_dispatch = True
    orchestrator = WriteBackOrchestrator(
        connector,
        journal,
        client_factory=lambda credentials: provider,
    )

    receipt = orchestrator.write_update_by_id(
        domain="company",
        record_id="42",
        canonical={"company_name": "Acme"},
        approval=_approval(connector, "op-indeterminate", {"name": "Acme"}),
        operation_id="op-indeterminate",
        correlation_id="correlation-indeterminate",
    )

    assert receipt.status == CRMWriteState.INDETERMINATE
    assert receipt.reconciliation_state == "required"
    assert journal.receipts["op-indeterminate"].status == CRMWriteState.INDETERMINATE
    assert journal.steps == ["prepare", "claim", "dispatched", "finalize"]


@pytest.mark.asyncio
async def test_sdk_handler_refuses_forged_payload_before_any_provider_io() -> None:
    result = await handle_writeback(
        "trusted-tenant",
        {
            "tenant": "forged-tenant",
            "crm_type": "hubspot",
            "credentials": {"access_token": "forged"},
            "mapping_path": "/tmp/forged.yaml",
            "policy_cleared": True,
        },
    )

    assert result["success"] is False
    assert result["status"] == "disabled"
    assert result["error"] == "writeback_disabled_pending_trusted_context_and_durable_journal"


def test_mcp_writeback_refuses_without_dispatch_and_preserves_arguments() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def dispatch(action: str, tenant: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((action, tenant, payload))
        return {"success": True}

    arguments = {"tenant": "forged-tenant", "record_id": "42", "enriched_data": {"x": "y"}}
    response = MCPServer(chassis_dispatch=dispatch).handle_request(
        "tools/call",
        {"name": "writeback", "arguments": arguments},
    )

    assert response["isError"] is True
    assert calls == []
    assert arguments["tenant"] == "forged-tenant"
