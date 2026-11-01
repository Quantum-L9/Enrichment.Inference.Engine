"""CRM integration package with a single provider registry owner."""

from .base import (
    CRMClientBase,
    CRMCredentials,
    CRMErrorCode,
    CRMJournalError,
    CRMLookupResult,
    CRMLookupStatus,
    CRMOperation,
    CRMOperationConflictError,
    CRMReadError,
    CRMRecord,
    CRMRecordRef,
    CRMType,
    CRMUniqueKey,
    CRMWriteIntent,
    CRMWritePermit,
    CRMWriteReceipt,
    CRMWriteState,
    WriteResult,
)
from .field_mapper import FieldMapper
from .hubspot_client import HubSpotClient
from .odoo_client import OdooClient
from .salesforce_client import SalesforceClient
from .writeback import (
    CRMActivationLevel,
    CRMJournalClaim,
    CRMOperationJournal,
    TrustedCRMConnector,
    TrustedWritebackApproval,
    WriteBackOrchestrator,
)

# This package is the sole owner of provider selection. The orchestrator obtains
# this mapping lazily after package initialization to avoid a circular import.
CLIENT_REGISTRY: dict[CRMType, type[CRMClientBase]] = {
    CRMType.ODOO: OdooClient,
    CRMType.SALESFORCE: SalesforceClient,
    CRMType.HUBSPOT: HubSpotClient,
}

__all__ = [
    "CLIENT_REGISTRY",
    "CRMActivationLevel",
    "CRMClientBase",
    "CRMCredentials",
    "CRMErrorCode",
    "CRMJournalClaim",
    "CRMJournalError",
    "CRMLookupResult",
    "CRMLookupStatus",
    "CRMOperation",
    "CRMOperationConflictError",
    "CRMOperationJournal",
    "CRMReadError",
    "CRMRecord",
    "CRMRecordRef",
    "CRMType",
    "CRMUniqueKey",
    "CRMWriteIntent",
    "CRMWritePermit",
    "CRMWriteReceipt",
    "CRMWriteState",
    "FieldMapper",
    "HubSpotClient",
    "OdooClient",
    "SalesforceClient",
    "TrustedCRMConnector",
    "TrustedWritebackApproval",
    "WriteBackOrchestrator",
    "WriteResult",
]
