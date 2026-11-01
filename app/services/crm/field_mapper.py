"""Provider-bound canonical-to-CRM mapping and safe field projection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .base import CRMType

_PROVIDER_OBJECT_KEYS: dict[CRMType, str] = {
    CRMType.ODOO: "odoo_model",
    CRMType.SALESFORCE: "salesforce_object",
    CRMType.HUBSPOT: "hubspot_object",
}

# These target fields identify or redirect records. R1 never maps them for a
# writeback update, even if a global legacy mapping happens to list one.
_PROTECTED_TARGET_FIELDS = {
    "id",
    "hs_object_id",
    "ownerid",
    "hubspot_owner_id",
    "user_id",
    "parent_id",
    "partner_id",
    "accountid",
    "associatedcompanyid",
}


@dataclass(frozen=True)
class ObjectMapping:
    """Mapping from one canonical domain to one provider object."""

    crm_object: str
    field_map: dict[str, str]
    extra_domain: list[list[Any]] | None = None


class FieldMapper:
    """Load one provider mapping and project only safe, approved field values."""

    def __init__(self, mapping_path: str | Path, expected_crm: CRMType | None = None) -> None:
        self.mapping_path = Path(mapping_path).resolve()
        with self.mapping_path.open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle)
        if not isinstance(config, dict):
            raise ValueError("CRM mapping must be a YAML object")
        configured_crm = config.get("crm")
        try:
            self.crm_type = CRMType(str(configured_crm))
        except ValueError as exc:
            raise ValueError("CRM mapping has an unsupported crm value") from exc
        if expected_crm is not None and self.crm_type != expected_crm:
            raise ValueError(
                f"CRM mapping provider {self.crm_type.value} does not match trusted connector "
                f"{expected_crm.value}"
            )
        self.version = str(config.get("version", ""))
        objects_config = config.get("objects", {})
        if not isinstance(objects_config, dict):
            raise ValueError("CRM mapping objects must be an object")
        self.custom_fields: dict[str, Any] = dict(config.get("custom_fields", {}))
        self._mappings: dict[str, ObjectMapping] = {}
        object_key = _PROVIDER_OBJECT_KEYS[self.crm_type]
        for domain, raw_mapping in objects_config.items():
            if not isinstance(domain, str) or not isinstance(raw_mapping, dict):
                raise ValueError("CRM mapping contains an invalid object declaration")
            crm_object = raw_mapping.get(object_key)
            field_map = raw_mapping.get("mappings", {})
            if not isinstance(crm_object, str) or not crm_object.strip():
                raise ValueError(f"CRM mapping for domain={domain} lacks {object_key}")
            if not isinstance(field_map, dict) or not all(
                isinstance(source, str) and isinstance(target, str)
                for source, target in field_map.items()
            ):
                raise ValueError(f"CRM mapping for domain={domain} has invalid field mappings")
            extra_domain = raw_mapping.get("extra_domain")
            if extra_domain is not None and not isinstance(extra_domain, list):
                raise ValueError(f"CRM mapping for domain={domain} has invalid extra_domain")
            self._mappings[domain] = ObjectMapping(
                crm_object=crm_object,
                field_map=dict(field_map),
                extra_domain=extra_domain,
            )

    def object_for_domain(self, domain: str) -> str:
        """Return the trusted provider object configured for the canonical domain."""
        mapping = self._mappings.get(domain)
        if mapping is None:
            raise ValueError(f"No CRM mapping for domain={domain}")
        return mapping.crm_object

    def to_crm(self, domain: str, canonical: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Translate non-empty canonical fields using the selected provider mapping.

        This compatibility method retains historical mapping behavior. New
        writeback code must use ``to_approved_crm`` so identity and ownership
        fields are excluded before a provider request is composed.
        """
        mapping = self._mapping_for_domain(domain)
        payload: dict[str, Any] = {}
        for source, target in mapping.field_map.items():
            if source in canonical and canonical[source] not in (None, ""):
                payload[target] = canonical[source]
        return mapping.crm_object, payload

    def to_approved_crm(
        self,
        domain: str,
        canonical: dict[str, Any],
        allowed_target_fields: set[str] | frozenset[str],
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        """Map only trusted-allowlisted, non-identity, non-ownership fields.

        ``allowed_target_fields`` comes from the trusted tenant connector
        binding, never from request data or a mapping path supplied by a caller.
        The returned skips are safe field names and reason codes, not values.
        """
        mapping = self._mapping_for_domain(domain)
        allowed = set(allowed_target_fields)
        payload: dict[str, Any] = {}
        skipped: dict[str, str] = {}
        for source, target in mapping.field_map.items():
            if source not in canonical or canonical[source] in (None, ""):
                continue
            if self._is_protected_mapping(source, target):
                skipped[source] = "identity_or_ownership_field"
                continue
            if target not in allowed:
                skipped[source] = "field_not_allowed_by_trusted_connector"
                continue
            payload[target] = canonical[source]
        return mapping.crm_object, payload, skipped

    def get_extra_domain(self, domain: str) -> list[list[Any]] | None:
        """Return legacy Odoo lookup clauses for compatibility-only callers."""
        mapping = self._mappings.get(domain)
        return mapping.extra_domain if mapping else None

    def get_custom_fields(self, model: str) -> list[dict[str, str]]:
        """Return declared custom-field definitions where the mapping uses that shape."""
        custom_fields = self.custom_fields.get(model, [])
        return list(custom_fields) if isinstance(custom_fields, list) else []

    def _mapping_for_domain(self, domain: str) -> ObjectMapping:
        """Resolve one declared domain mapping or fail before provider I/O."""
        mapping = self._mappings.get(domain)
        if mapping is None:
            raise ValueError(f"No CRM mapping for domain={domain}")
        return mapping

    def _is_protected_mapping(self, source: str, target: str) -> bool:
        """Identify R1-prohibited identity, owner, and relationship mappings."""
        source_folded = source.casefold()
        target_folded = target.casefold()
        if source_folded.endswith("_id"):
            return True
        if target_folded.endswith("_id"):
            return True
        if "owner" in source_folded or "owner" in target_folded:
            return True
        if source_folded.endswith("_company_id") or "parent" in target_folded:
            return True
        return target_folded in _PROTECTED_TARGET_FIELDS
