"""
Canonical → CRM field mapping loader and transformer.

Reads a YAML mapping file and translates canonical enrichment fields
to CRM-native field names. Supports Odoo, Salesforce, and HubSpot
object key conventions.

L9 Architecture Note:
    This module is chassis-agnostic. It never imports FastAPI.
    It is called by WriteBackOrchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml


@dataclass
class ObjectMapping:
    """Mapping from canonical domain to a specific CRM object."""

    crm_object: str
    field_map: dict[str, str]
    extra_domain: list[list[Any]] | None = None


class FieldMapper:
    """
    Loads a YAML mapping file and provides canonical → CRM field
    translation for any supported CRM platform.
    """

    def __init__(self, mapping_path: str) -> None:
        with open(mapping_path) as f:
            cfg = yaml.safe_load(f)

        self.crm: str = cfg.get("crm", "unknown")
        self.version: str = cfg.get("version", "1.0")
        self.objects_cfg: dict[str, Any] = cfg.get("objects", {})
        self.custom_fields: dict[str, Any] = cfg.get("custom_fields", {})

        self._mappings: dict[str, ObjectMapping] = {}
        for dom, ocfg in self.objects_cfg.items():
            # Support all CRM platform keys
            crm_object = (
                ocfg.get("odoo_model")
                or ocfg.get("salesforce_object")
                or ocfg.get("hubspot_object")
                or ""
            )
            self._mappings[dom] = ObjectMapping(
                crm_object=crm_object,
                field_map=ocfg.get("mappings", {}),
                extra_domain=ocfg.get("extra_domain"),
            )

    def to_crm(
        self,
        domain: str,
        canonical: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """
        Translate canonical fields to CRM-native fields.

        Returns:
            (crm_object_name, crm_payload)
        """
        mapping = self._mappings.get(domain)
        if not mapping:
            msg = f"No CRM mapping for domain={domain}"
            raise ValueError(msg)

        crm_payload: dict[str, Any] = {}
        for src, dest in mapping.field_map.items():
            if src in canonical and canonical[src] not in (None, ""):
                crm_payload[dest] = canonical[src]

        return mapping.crm_object, crm_payload

    def get_extra_domain(self, domain: str) -> list[list[Any]] | None:
        """Return Odoo-style extra domain filters for a mapping."""
        mapping = self._mappings.get(domain)
        return mapping.extra_domain if mapping else None

    def get_custom_fields(self, model: str) -> list[dict[str, str]]:
        """Return custom field definitions for a CRM model."""
        custom: list[dict[str, str]] = self.custom_fields.get(model, [])
        return custom
