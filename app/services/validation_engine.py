"""
Schema-aware validation and normalization.

Audit fixes applied:
  - H15: Accepts ARBITRARY target schemas, not hardcoded plastics fields.
  - H16: Preserves ordering via dict.fromkeys() instead of set().
  - M15: Partial validation — keeps valid fields, skips invalid ones.
         No longer all-or-nothing.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import structlog

from app.engines.spec_types import value_defects
from app.utils.safe_convert import normalize_confidence

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

# Declared scalar types whose values are checked, not coerced: an enum against
# its declared ``values``, a date / datetime / duration as ISO-8601 text. The
# check is ``spec_types.value_defects`` -- the grammar the graph row enforces --
# so a value admitted here is one the row would accept (EIE-D-003).
CHECKED_SCALAR_TYPES = frozenset({"enum", "date", "datetime", "duration"})


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
    Always extracts confidence through ``normalize_confidence``: clamped to
    [0.0, 1.0], and 0.0 when missing or malformed.

    Returns normalized dict with 'confidence' key guaranteed.
    Raises ValidationError only if payload is not a dict at all.
    """
    if not isinstance(payload, dict):
        raise ValidationError("response_not_dict")

    # ── Extract confidence ───────────────────────────
    # The provider's own confidence is an uncalibrated claim. Missing or
    # malformed (bool, unparseable, NaN, ±Inf) is 0.0, never a default that
    # could clear a threshold; finite values are clamped (post-#320 F1).
    confidence = normalize_confidence(payload.get("confidence"))

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

            field_type_str, items = _declared_type(declared)
            if field_type_str.lower() == "map":
                # A declared map is a mapping restricted to its declared
                # properties, with required ones present -- never accepted as-is.
                try:
                    if not isinstance(value, Mapping):
                        raise TypeError("declared map is not a mapping")
                    decl = declared if isinstance(declared, Mapping) else None
                    normalized[field_name] = _coerce_map(value, decl)
                except (ValueError, TypeError):
                    logger.debug(
                        "field_coercion_skipped",
                        field=field_name,
                        expected=field_type_str,
                        got_type=type(value).__name__,
                    )
                continue
            if field_type_str.lower() in CHECKED_SCALAR_TYPES:
                try:
                    normalized[field_name] = _checked_scalar(value, declared, field_name)
                except ValueError:
                    logger.debug(
                        "field_coercion_skipped",
                        field=field_name,
                        expected=field_type_str,
                        got_type=type(value).__name__,
                    )
                continue
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
                # Dedup preserving order; a mapping member stays a mapping (F05)
                normalized[k] = _dedup([_free_member(i) for i in v[:50] if i])
            else:
                normalized[k] = v

    return normalized


def _declared_type(
    declared: str | Mapping[str, Any],
) -> tuple[str, Mapping[str, Any] | None]:
    """A schema value as (type name, declared list items): a name or a typed declaration."""
    if not isinstance(declared, Mapping):
        return declared, None
    items = declared.get("items")
    return str(declared.get("type", "")), items if isinstance(items, Mapping) else None


def _checked_scalar(value: Any, declared: str | Mapping[str, Any], path: str) -> Any:
    """``value`` unchanged when it conforms to a checked scalar declaration, else ValueError.

    ``declared`` is a type name (``"date"``) or a declaration carrying ``values``
    for an enum. A bare ``"enum"`` with no ``values`` admits any string, as
    ``spec_types`` does; it never admits a non-string. Text is trimmed first
    unless it is a declared member as written, as every other string member is, so ``"A, B"`` splits to ``A`` and ``B``.
    """
    decl = dict(declared) if isinstance(declared, Mapping) else {"type": declared}
    decl["type"] = str(decl.get("type", "")).lower()
    decl.pop("required", None)  # an absent value never reaches here
    members = decl.get("values")
    if isinstance(value, str) and not (isinstance(members, list) and value in members):
        # A declared member is matched as written; only other text is trimmed.
        value = value.strip()
    defects = value_defects(value, decl, path)
    if defects:
        raise ValueError(str(defects[0]))
    return value


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
        members = _list_members(value, declared_items=items is not None)
        return _dedup([_coerce_member(v, items) for v in members])

    if expected in (int, float):
        return _number(value, expected)

    # str
    return str(value).strip()[:4096]


def _number(value: Any, expected: type) -> int | float:
    """A finite number of the declared kind; never a bool, NaN, ±Inf or a truncation.

    ``float(True)`` is ``1.0`` and ``int(3.7)`` is ``3``: each would admit a value
    the provider never stated. Such values raise, so the field is skipped. An
    integer too large for a float stays a valid ``int``; as a ``float`` it is
    refused rather than raising ``OverflowError`` out of validation.
    """
    if isinstance(value, bool):
        raise TypeError("a boolean is not a number")
    if expected is int:
        if isinstance(value, float) and not value.is_integer():
            raise ValueError("a fractional or non-finite number is not an integer")
        return int(value)
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("not a representable number") from exc
    if not math.isfinite(number):
        raise ValueError("not a finite number")
    return number


def _free_member(value: Any) -> Any:
    """One member of a schema-free list: a mapping stays a bounded mapping, else trimmed text."""
    if isinstance(value, Mapping):
        items = [(key, member) for key, member in value.items() if isinstance(key, str)][:50]
        return {
            key: member.strip()[:256] if isinstance(member, str) else member
            for key, member in items
        }
    return str(value).strip()[:256]


def _list_members(value: Any, *, declared_items: bool = False) -> list[Any]:
    """The members a list field carries: CSV text split, empties dropped.

    With ``declared_items`` only absent members are dropped: ``None`` and blank
    text carry no value. Every other member reaches its declared item type, so
    ``0`` and ``0.0`` are kept, a boolean in a numeric list is refused, and an
    empty mapping reaches required-property validation. An untyped list keeps
    the falsy filter.
    """
    if isinstance(value, str):
        return [s for s in value.split(",") if s.strip()]
    if isinstance(value, (list, tuple)):
        if declared_items:
            return [v for v in value if not _is_absent(v)]
        return [v for v in value if v]
    return [value]


def _is_absent(value: Any) -> bool:
    """A list member that states no value: ``None`` or blank text."""
    return value is None or (isinstance(value, str) and not value.strip())


def _coerce_member(value: Any, items: Mapping[str, Any] | None) -> Any:
    """One list member: by its declared item type, else by its own shape."""
    item_type = str(items.get("type", "")).lower() if items else ""
    if item_type == "map" or (not item_type and isinstance(value, Mapping)):
        if not isinstance(value, Mapping):
            raise TypeError("declared map item is not a mapping")
        return _coerce_map(value, items)
    if items is not None and item_type in CHECKED_SCALAR_TYPES:
        return _checked_scalar(value, items, "item")
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
        if not isinstance(name, str):
            continue
        if value.get(name) is not None:
            result[name] = _coerce_property(value[name], prop)
        elif prop.get("required") is True:
            # A member without a required property is incomplete, never a fact (F22).
            raise TypeError(f"declared map property {name} is required")
    return result


def _coerce_property(value: Any, prop: Mapping[str, Any]) -> Any:
    """One declared property of a map member, by its declared type."""
    type_name = str(prop.get("type", "")).lower()
    if type_name == "map":
        if not isinstance(value, Mapping):
            raise TypeError(f"declared map property {prop.get('name')} is not a mapping")
        return _coerce_map(value, prop)
    if type_name in CHECKED_SCALAR_TYPES:
        return _checked_scalar(value, prop, str(prop.get("name")))
    expected = TYPE_MAP.get(type_name)
    if expected is None:
        return value
    nested = prop.get("items")
    return _coerce(value, expected, nested if isinstance(nested, Mapping) else None)


def _dedup(members: list[Any]) -> list[Any]:
    """Order-preserving de-duplication that also handles unhashable mappings."""
    seen: list[Any] = []
    for member in members:
        if member not in seen:
            seen.append(member)
    return seen
