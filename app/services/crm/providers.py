"""Well-known CRM provider keys.

The writeback layer is provider-agnostic: it selects an adapter by the string
key a trusted connector names, through the registry in ``app.services.crm.base``.
This enum exists only so callers that integrate a built-in adapter can spell its
key without a bare string literal. A provider that is not listed here is still
first-class once its adapter module registers itself with
``register_crm_client``; nothing in the orchestrator, the mapper, or the
contracts consults this enum.
"""

from __future__ import annotations

from enum import StrEnum


class CRMType(StrEnum):
    """Keys of the adapters this repository ships. Not a closed set."""

    ODOO = "odoo"
    SALESFORCE = "salesforce"
    HUBSPOT = "hubspot"
