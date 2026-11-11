"""CRM integration package.

Provider selection is a registry keyed by provider string: ``base.CLIENT_REGISTRY``
through ``register_crm_client`` / ``resolve_crm_client``. Each shipped adapter
module registers itself when imported, and this package imports them so their
registrations run at package import. Adding a provider is one adapter module
that calls ``register_crm_client`` plus one mapping file under ``config/crm/``.
"""

from .base import (
    CLIENT_REGISTRY,
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
    crm_provider_key,
    register_crm_client,
    registered_crm_providers,
    resolve_crm_client,
    unregister_crm_client,
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
    "crm_provider_key",
    "register_crm_client",
    "registered_crm_providers",
    "resolve_crm_client",
    "unregister_crm_client",
]
