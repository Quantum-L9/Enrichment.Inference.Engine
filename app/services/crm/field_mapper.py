"""Provider-bound canonical-to-CRM mapping and safe field projection.

One mapping file describes one provider. Its shape is the same for every
provider, so the mapper never needs to know which one it is loading::

    crm: <provider key, matches the adapter registration>
    version: "2.0"
    protected_fields:        # optional: provider field names never written
      - hs_object_id
    objects:
      <canonical domain>:
        object: <provider object/model name>
        mappings:
          <canonical field>: <provider field>

The ``object`` key is the only accepted object declaration. Provider-specific
spellings (``<provider>_model`` / ``<provider>_object``) are rejected with an
error that names the domain, so an unconverted mapping fails before any write.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .base import crm_provider_key

OBJECT_KEY = "object"
PROTECTED_FIELDS_KEY = "protected_fields"

# Target names that identify a record in every provider this layer has met.
# Provider-specific identity, owner and association fields are declared by
# each mapping file under ``protected_fields``; they do not belong here.
_GENERIC_PROTECTED_TARGETS = frozenset({"id"})


@dataclass(frozen=True)
class ObjectMapping:
    """Mapping from one canonical domain to one provider object."""

    crm_object: str
    field_map: dict[str, str]


class FieldMapper:
    """Load one provider mapping and project only safe, approved field values."""

    def __init__(self, mapping_path: str | Path, expected_crm: str | None = None) -> None:
        self.mapping_path = Path(mapping_path).resolve()
        with self.mapping_path.open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle)
        if not isinstance(config, dict):
            raise ValueError("CRM mapping must be a YAML object")
        try:
            self.crm_type: str = crm_provider_key(config.get("crm"))
        except ValueError as exc:
            raise ValueError("CRM mapping must declare a non-empty crm provider key") from exc
        if expected_crm is not None and self.crm_type != crm_provider_key(expected_crm):
            raise ValueError(
                f"CRM mapping provider {self.crm_type!r} does not match trusted connector "
                f"{crm_provider_key(expected_crm)!r}"
            )
        self.version = str(config.get("version", ""))
        self.protected_targets: frozenset[str] = _load_protected_fields(config)
        objects_config = config.get("objects", {})
        if not isinstance(objects_config, dict):
            raise ValueError("CRM mapping objects must be an object")
        raw_custom_fields = config.get("custom_fields", {})
        self.custom_fields: dict[str, Any] = (
            dict(raw_custom_fields) if isinstance(raw_custom_fields, dict) else {}
        )
        self._mappings: dict[str, ObjectMapping] = {}
        for domain, raw_mapping in objects_config.items():
            if not isinstance(domain, str) or not isinstance(raw_mapping, dict):
                raise ValueError("CRM mapping contains an invalid object declaration")
            self._mappings[domain] = _load_object_mapping(domain, raw_mapping)

    def object_for_domain(self, domain: str) -> str:
        """Return the trusted provider object configured for the canonical domain."""
        return self._mapping_for_domain(domain).crm_object

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
        """Identify identity, owner and relationship mappings that are never written.

        The generic rules apply to every provider: a field whose name ends in
        ``_id``, or names an owner or parent, redirects or re-identifies a
        record. Anything provider-specific comes from the mapping file's
        ``protected_fields`` list.
        """
        source_folded = source.casefold()
        target_folded = target.casefold()
        if source_folded.endswith("_id") or target_folded.endswith("_id"):
            return True
        if "owner" in source_folded or "owner" in target_folded:
            return True
        if "parent" in target_folded:
            return True
        if target_folded in _GENERIC_PROTECTED_TARGETS:
            return True
        return target_folded in self.protected_targets


def _load_protected_fields(config: dict[str, Any]) -> frozenset[str]:
    raw = config.get(PROTECTED_FIELDS_KEY, [])
    if raw is None:
        return frozenset()
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"CRM mapping {PROTECTED_FIELDS_KEY} must be a list of field names")
    return frozenset(item.casefold() for item in raw)


def _load_object_mapping(domain: str, raw_mapping: dict[str, Any]) -> ObjectMapping:
    crm_object = raw_mapping.get(OBJECT_KEY)
    if crm_object is None:
        legacy_keys = sorted(
            key
            for key in raw_mapping
            if isinstance(key, str) and (key.endswith("_model") or key.endswith("_object"))
        )
        if legacy_keys:
            raise ValueError(
                f"CRM mapping for domain={domain} uses a provider-specific object key "
                f"{legacy_keys[0]!r}; declare the provider object under {OBJECT_KEY!r}"
            )
        raise ValueError(f"CRM mapping for domain={domain} lacks {OBJECT_KEY!r}")
    if not isinstance(crm_object, str) or not crm_object.strip():
        raise ValueError(f"CRM mapping for domain={domain} has an empty {OBJECT_KEY!r}")
    field_map = raw_mapping.get("mappings", {})
    if not isinstance(field_map, dict) or not all(
        isinstance(source, str) and isinstance(target, str) for source, target in field_map.items()
    ):
        raise ValueError(f"CRM mapping for domain={domain} has invalid field mappings")
    return ObjectMapping(crm_object=crm_object, field_map=dict(field_map))
