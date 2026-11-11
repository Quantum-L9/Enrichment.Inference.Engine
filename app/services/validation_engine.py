"""
Schema-aware validation and normalization.

Audit fixes applied:
  - H15: Accepts ARBITRARY target schemas, not hardcoded plastics fields.
  - H16: Preserves ordering via dict.fromkeys() instead of set().
  - M15: Partial validation — keeps valid fields, skips invalid ones.
         No longer all-or-nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog

logger = structlog.get_logger("validation_engine")


class ValidationError(Exception):
    pass


# Type coercion map — covers every type Salesforce and Odoo use
TYPE_MAP: dict[str, type] = {
    "string": str,
    "text": str,
    "char": str,
    "float": float,
    "number": float,
    "decimal": float,
    "currency": float,
    "integer": int,
    "int": int,
    "boolean": bool,
    "bool": bool,
    "list": list,
    "array": list,
}


def validate_response(
    payload: dict[str, Any],
    target_schema: Mapping[str, str | Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Validate and normalize an LLM enrichment response.

    With schema: coerces each field to declared type, skips failures. A schema
    value is a type name (``"float"``, ``"list"``) or a typed declaration in the
    shared spec syntax (``{"type": "list", "items": {"type": "map", ...}}``,
    ``app.engines.spec_types``); a declared list keeps its declared item type.
    Without schema: passes through with basic normalization.
    Always extracts and clamps confidence to [0.0, 1.0].

    Returns normalized dict with 'confidence' key guaranteed.
    Raises ValidationError only if payload is not a dict at all.
    """
    if not isinstance(payload, dict):
        raise ValidationError("response_not_dict")

    # ── Extract confidence ───────────────────────────
    raw_conf = payload.get("confidence")
    try:
        confidence = max(0.0, min(1.0, float(raw_conf))) if raw_conf is not None else 0.5
    except (ValueError, TypeError):
        confidence = 0.5

    normalized: dict[str, Any] = {"confidence": confidence}

    # ── Extract fields (nested or top-level) ─────────
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        # LLM might put fields at top level
        fields = {
            k: v
            for k, v in payload.items()
            if k not in ("confidence", "reasoning", "sources", "citations", "explanation")
        }

    if not fields:
        return normalized

    # ── Schema-driven validation ─────────────────────
    if target_schema:
        for field_name, declared in target_schema.items():
            if field_name == "confidence":
                continue

            value = fields.get(field_name)
            if value is None:
                continue

            items: Mapping[str, Any] | None = None
            if isinstance(declared, Mapping):
                field_type_str = str(declared.get("type", ""))
                raw_items = declared.get("items")
                items = raw_items if isinstance(raw_items, Mapping) else None
            else:
                field_type_str = declared
            expected = TYPE_MAP.get(field_type_str.lower())
            if expected is None:
                # Unknown type — accept as-is
                normalized[field_name] = value
                continue

            try:
                normalized[field_name] = _coerce(value, expected, items)
            except (ValueError, TypeError):
                logger.debug(
                    "field_coercion_skipped",
                    field=field_name,
                    expected=field_type_str,
                    got_type=type(value).__name__,
                )
                continue
    else:
        # No schema — basic normalization pass-through
        for k, v in fields.items():
            if isinstance(v, str):
                normalized[k] = v.strip()[:4096]
            elif isinstance(v, list):
                # Dedup preserving order
                normalized[k] = list(dict.fromkeys(str(i).strip()[:256] for i in v[:50] if i))
            else:
                normalized[k] = v

    return normalized


def _coerce(value: Any, expected: type, items: Mapping[str, Any] | None = None) -> Any:
    """Coerce a value to the expected type.

    A list keeps its members' shape: a mapping member stays a mapping (it is
    never stringified into ``"{'code': 'PET'}"``), and when the list's ``items``
    are declared every member is coerced to that declared item type.
    """
    if expected is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "yes", "1", "y")
        return bool(value)

    if expected is list:
        if isinstance(value, str):
            members: list[Any] = [s for s in value.split(",") if s.strip()]
        elif isinstance(value, (list, tuple)):
            members = [v for v in value if v]
        else:
            members = [value]
        return _dedup([_coerce_member(v, items) for v in members])

    if expected in (int, float):
        return expected(value)

    # str
    return str(value).strip()[:4096]


def _coerce_member(value: Any, items: Mapping[str, Any] | None) -> Any:
    """One list member: by its declared item type, else by its own shape."""
    item_type = str(items.get("type", "")).lower() if items else ""
    if item_type == "map" or (not item_type and isinstance(value, Mapping)):
        if not isinstance(value, Mapping):
            raise TypeError("declared map item is not a mapping")
        return _coerce_map(value, items)
    expected = TYPE_MAP.get(item_type, str)
    if expected is list:
        raise TypeError("a list of lists is not declarable")
    if expected is str:
        return str(value).strip()[:256]
    return _coerce(value, expected)


def _coerce_map(value: Mapping[str, Any], decl: Mapping[str, Any] | None) -> dict[str, Any]:
    """A mapping member; with declared properties, each declared key keeps its declared type."""
    properties = decl.get("properties") if decl else None
    if not isinstance(properties, list):
        return dict(value)
    result: dict[str, Any] = {}
    for prop in properties:
        name = prop.get("name") if isinstance(prop, Mapping) else None
        if not isinstance(name, str) or value.get(name) is None:
            continue
        expected = TYPE_MAP.get(str(prop.get("type", "")).lower())
        nested = prop.get("items") if isinstance(prop.get("items"), Mapping) else None
        if str(prop.get("type", "")).lower() == "map":
            if not isinstance(value[name], Mapping):
                raise TypeError(f"declared map property {name} is not a mapping")
            result[name] = _coerce_map(value[name], prop)
        elif expected is None:
            result[name] = value[name]
        else:
            result[name] = _coerce(value[name], expected, nested)
    return result


def _dedup(members: list[Any]) -> list[Any]:
    """Order-preserving de-duplication that also handles unhashable mappings."""
    seen: list[Any] = []
    for member in members:
        if member not in seen:
            seen.append(member)
    return seen
