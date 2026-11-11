"""Typed property declarations — the syntax EIE and CEG share for a tenant spec.

One declaration grammar (owner decision 2026-11-11; Cognitive.Engine.Graphs adopts
the identical syntax in its typed-contract change)::

    - {name: certifications, type: list, items: {type: string}}
    - name: processed_polymers
      type: list
      items:
        type: map
        properties:
          - {name: code, type: string, required: true}
    - name: location
      type: map
      properties:
        - {name: lat, type: float}

Rules: ``items`` is required iff the type is ``list`` (primitive, enum or map
items; never a list of lists); ``properties`` is required iff the type is
``map``. The declaration is the row contract: :func:`value_defects` checks a
value against it and :func:`encode_value` produces the wire value by the
declared type, so a ``list<map>`` travels as a real list of mappings and never
as a JSON string.

L9_META:
  tier: 2
  domain: graph-sync
  authority: owner decision 2026-11-11 (typed spec contract)
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

# CEG's PropertyType vocabulary (engine/config/schema.py) — the shared scalars.
SCALAR_TYPES = frozenset({"string", "int", "float", "bool", "datetime", "date", "duration", "enum"})
# EIE-native scalar spellings admitted before the shared syntax. Accepted by
# native validation for authored/proposed EIE specs; the cross-engine row
# contract (eie-ceg-sync/v2) compares the literal spelling, so a property that
# rides a sync row must use CEG's spelling.
SCALAR_ALIASES = {"integer": "int", "boolean": "bool", "number": "float"}
LIST = "list"
MAP = "map"
# Untyped containers retired by the shared syntax: they declare no item or
# property shape, so no row could be checked against them.
_RETIRED = {
    "dict": "declare `type: map` with `properties`",
    "object": "declare `type: map` with `properties`",
}
_IDENTIFIER = re.compile(r"^[A-Za-z_]\w*$", re.ASCII)
# One ISO-8601 duration component, e.g. ``12D`` or ``1.5S``; see _is_iso_duration.
_DURATION_UNIT = re.compile(r"(\d+(?:\.\d+)?)([A-Z])")


class TypeDeclarationError(ValueError):
    """A property declaration that does not follow the shared typed syntax."""


def _scalar(type_name: str) -> str:
    return SCALAR_ALIASES.get(type_name, type_name)


def validate_declaration(decl: object, context: str, *, item: bool = False) -> None:
    """Refuse a declaration outside the shared syntax; ``item`` marks a list's ``items``."""
    if not isinstance(decl, Mapping) or any(not isinstance(k, str) for k in decl):
        raise TypeDeclarationError(f"{context} must be a string-keyed mapping")
    type_name = decl.get("type")
    if not isinstance(type_name, str):
        raise TypeDeclarationError(f"{context}.type must be a string")
    if type_name in _RETIRED:
        raise TypeDeclarationError(
            f"{context}: type {type_name!r} is not a typed declaration; {_RETIRED[type_name]}"
        )
    if "required" in decl and type(decl["required"]) is not bool:
        raise TypeDeclarationError(f"{context}.required must be boolean")
    validator = _DECLARATION_VALIDATORS.get(type_name, _validate_scalar_declaration)
    validator(decl, context, item)


def _validate_list_declaration(decl: Mapping[str, Any], context: str, item: bool) -> None:
    if item:
        raise TypeDeclarationError(f"{context}: a list of lists is not declarable")
    if "properties" in decl:
        raise TypeDeclarationError(f"{context}: `properties` belongs to a map, not a list")
    if "items" not in decl:
        raise TypeDeclarationError(
            f"{context}: `type: list` requires `items` (e.g. `items: {{type: string}}`)"
        )
    validate_declaration(decl["items"], f"{context}.items", item=True)


def _validate_map_declaration(decl: Mapping[str, Any], context: str, _item: bool) -> None:
    if "items" in decl:
        raise TypeDeclarationError(f"{context}: `items` belongs to a list, not a map")
    properties = decl.get("properties")
    if not isinstance(properties, list) or not properties:
        raise TypeDeclarationError(f"{context}: `type: map` requires nonempty `properties`")
    seen: set[str] = set()
    for entry in properties:
        name = _map_property_name(entry, context)
        if name in seen:
            raise TypeDeclarationError(f"{context}.properties: duplicate {name}")
        seen.add(name)
        validate_declaration(entry, f"{context}.{name}")


def _map_property_name(entry: object, context: str) -> str:
    if not isinstance(entry, Mapping):
        raise TypeDeclarationError(f"{context}.properties entries must be mappings")
    name = entry.get("name")
    if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
        raise TypeDeclarationError(f"{context}.properties name must be an identifier")
    return name


def _validate_scalar_declaration(decl: Mapping[str, Any], context: str, _item: bool) -> None:
    type_name = str(decl["type"])
    if _scalar(type_name) not in SCALAR_TYPES:
        raise TypeDeclarationError(f"{context}: unsupported type {type_name!r}")
    if "items" in decl:
        raise TypeDeclarationError(f"{context}: `items` is only declarable on a list")
    if "properties" in decl:
        raise TypeDeclarationError(f"{context}: `properties` is only declarable on a map")
    if "values" in decl and not _is_string_list(decl["values"]):
        raise TypeDeclarationError(f"{context}.values must be a nonempty list of strings")


def _is_string_list(values: object) -> bool:
    return isinstance(values, list) and bool(values) and all(isinstance(v, str) for v in values)


_DECLARATION_VALIDATORS: dict[str, Callable[[Mapping[str, Any], str, bool], None]] = {
    LIST: _validate_list_declaration,
    MAP: _validate_map_declaration,
}


def is_container(decl: Mapping[str, Any]) -> bool:
    """Whether a declaration is a map or a list of maps (CEG: childsync wire shapes)."""
    type_name = decl.get("type")
    if type_name == MAP:
        return True
    items = decl.get("items")
    return type_name == LIST and isinstance(items, Mapping) and items.get("type") == MAP


def is_scalar_or_scalar_list(decl: Mapping[str, Any]) -> bool:
    """Whether a declaration is a scalar or a list of scalars (taxonomy-edge fields)."""
    type_name = decl.get("type")
    if type_name == LIST:
        items = decl.get("items")
        return isinstance(items, Mapping) and items.get("type") not in {LIST, MAP}
    return type_name != MAP


def shape(decl: Mapping[str, Any]) -> dict[str, Any]:
    """The comparable typed shape of a declaration: type, required, enum values, nesting.

    Descriptions, units and other prose are not part of the row contract. The
    type is kept as spelled, so ``integer`` and ``int`` are different shapes.
    """
    result: dict[str, Any] = {
        "type": decl.get("type"),
        "required": bool(decl.get("required", False)),
    }
    if decl.get("type") == "enum" and "values" in decl:
        result["values"] = list(decl["values"])
    items = decl.get("items")
    if isinstance(items, Mapping):
        result["items"] = shape(items)
    properties = decl.get("properties")
    if isinstance(properties, list):
        result["properties"] = {
            entry["name"]: shape(entry)
            for entry in properties
            if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)
        }
    return result


def describe(decl: Mapping[str, Any]) -> str:
    """A compact human name for a declaration: ``list<map{code}>``, ``float``."""
    type_name = decl.get("type")
    items = decl.get("items")
    if type_name == LIST and isinstance(items, Mapping):
        return f"list<{describe(items)}>"
    properties = decl.get("properties")
    if type_name == MAP and isinstance(properties, list):
        names = ",".join(str(p.get("name")) for p in properties if isinstance(p, Mapping))
        return f"map{{{names}}}"
    return str(type_name)


def actual_type(value: object) -> str:
    """A compact human name for a runtime value's shape."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "map"
    if isinstance(value, (list, tuple)):
        kinds = sorted({actual_type(v) for v in value})
        return f"list<{'|'.join(kinds) or 'empty'}>"
    return type(value).__name__


@dataclass(frozen=True, slots=True)
class TypeDefect:
    """One value that does not match its declaration."""

    field: str
    declared: str
    actual: str
    reason: str

    def __str__(self) -> str:
        return f"{self.field}: declared {self.declared}, got {self.actual} ({self.reason})"


def _check_string(value: object, _decl: Mapping[str, Any]) -> str | None:
    return None if isinstance(value, str) else "not a string"


def _check_bool(value: object, _decl: Mapping[str, Any]) -> str | None:
    return None if isinstance(value, bool) else "not a boolean"


def _check_int(value: object, _decl: Mapping[str, Any]) -> str | None:
    return None if isinstance(value, int) and not isinstance(value, bool) else "not an integer"


def _check_float(value: object, _decl: Mapping[str, Any]) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "not a number"
    return None if _as_float(value) is not None else "not a finite number"


def _check_enum(value: object, decl: Mapping[str, Any]) -> str | None:
    if not isinstance(value, str):
        return "not a string"
    allowed = decl.get("values")
    return None if not allowed or value in allowed else f"not one of {allowed}"


def _check_datetime(value: object, _decl: Mapping[str, Any]) -> str | None:
    if isinstance(value, datetime) or (isinstance(value, str) and _iso(value, datetime)):
        return None
    return "not an ISO datetime"


def _check_date(value: object, _decl: Mapping[str, Any]) -> str | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return None
    return None if isinstance(value, str) and _iso(value, date) else "not an ISO date"


def _check_duration(value: object, _decl: Mapping[str, Any]) -> str | None:
    return None if isinstance(value, str) and _is_iso_duration(value) else "not ISO-8601"


_SCALAR_CHECKS: dict[str, Callable[[object, Mapping[str, Any]], str | None]] = {
    "string": _check_string,
    "bool": _check_bool,
    "int": _check_int,
    "float": _check_float,
    "enum": _check_enum,
    "datetime": _check_datetime,
    "date": _check_date,
    "duration": _check_duration,
}


def _scalar_ok(type_name: str, value: object, decl: Mapping[str, Any]) -> str | None:
    """None when ``value`` is a valid ``type_name`` scalar, else the reason."""
    check = _SCALAR_CHECKS.get(_scalar(type_name))
    if check is None:
        return f"undeclarable type {type_name!r}"
    return check(value, decl)


def _is_iso_duration(text: str) -> bool:
    """An ISO-8601 duration: ``[-]P[nY][nM][nW][nD][T[nH][nM][n[.n]S]]``, one component at least."""
    body = text.removeprefix("-")
    if not body.startswith("P"):
        return False
    date_part, _, time_part = body[1:].partition("T")
    if not date_part and not time_part:  # "P" and "PT" carry no component
        return False
    return _duration_units(date_part, "YMWD") and _duration_units(time_part, "HMS")


def _duration_units(text: str, order: str) -> bool:
    """Components of one duration part, each unit at most once and in ``order``."""
    position, last = 0, -1
    for match in _DURATION_UNIT.finditer(text):
        index = order.find(match.group(2))
        fractional = "." in match.group(1)
        if match.start() != position or index <= last or (fractional and match.group(2) != "S"):
            return False
        position, last = match.end(), index
    return position == len(text)


def _iso(text: str, kind: type[date]) -> bool:
    try:
        kind.fromisoformat(text.replace("Z", "+00:00") if kind is datetime else text)
    except ValueError:
        return False
    return True


def value_defects(value: object, decl: Mapping[str, Any], path: str) -> list[TypeDefect]:
    """Every place ``value`` departs from ``decl``; empty when it conforms.

    ``None`` conforms to a non-required declaration (a cleared property). Keys a
    map value carries beyond its declared properties are not defects: the
    encoder drops them (:func:`encode_value`), as CEG drops undeclared keys.
    """
    if value is None:
        required = bool(decl.get("required"))
        return [TypeDefect(path, describe(decl), "null", "required")] if required else []
    type_name = decl.get("type")
    if type_name == LIST:
        return _list_defects(value, decl, path)
    if type_name == MAP:
        return _map_defects(value, decl, path)
    reason = _scalar_ok(str(type_name), value, decl)
    return [] if reason is None else [TypeDefect(path, describe(decl), actual_type(value), reason)]


def _list_defects(value: object, decl: Mapping[str, Any], path: str) -> list[TypeDefect]:
    if not isinstance(value, (list, tuple)):
        return [TypeDefect(path, describe(decl), actual_type(value), "not a list")]
    items = decl.get("items", {})
    defects: list[TypeDefect] = []
    for index, member in enumerate(value):
        where = f"{path}[{index}]"
        if member is None:
            defects.append(TypeDefect(where, describe(items), "null", "null item"))
        else:
            defects.extend(value_defects(member, items, where))
    return defects


def _map_defects(value: object, decl: Mapping[str, Any], path: str) -> list[TypeDefect]:
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
        return [TypeDefect(path, describe(decl), actual_type(value), "not a string-keyed map")]
    defects: list[TypeDefect] = []
    for prop in decl.get("properties", []):
        where = f"{path}.{prop['name']}"
        child = value.get(prop["name"])
        if child is None and prop.get("required"):
            defects.append(TypeDefect(where, describe(prop), "missing", "required"))
        else:
            defects.extend(value_defects(child, prop, where))
    return defects


def encode_value(value: Any, decl: Mapping[str, Any]) -> Any:
    """The wire value for a conforming ``value``: encoded by its declared type.

    Lists stay lists, maps stay mappings restricted to their declared
    properties, datetimes/dates travel as ISO strings. Call only after
    :func:`value_defects` returned no defect.
    """
    if value is None:
        return None
    type_name = decl.get("type")
    if type_name == LIST:
        items = decl.get("items", {})
        return [encode_value(member, items) for member in value]
    if type_name == MAP:
        return {
            prop["name"]: encode_value(value[prop["name"]], prop)
            for prop in decl.get("properties", [])
            if prop["name"] in value
        }
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if _scalar(str(type_name)) == "float" and isinstance(value, int):
        converted = _as_float(value)
        return value if converted is None else converted
    return value


def _as_float(value: int | float) -> float | None:
    """``value`` as a finite float, else None (an int too large for a float)."""
    try:
        converted = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return converted if math.isfinite(converted) else None


def node_properties(spec: Mapping[str, Any], label: str) -> list[dict[str, Any]] | None:
    """The property declarations of ``label`` in a spec's ``ontology.nodes`` list, else None."""
    ontology = spec.get("ontology")
    nodes = ontology.get("nodes") if isinstance(ontology, Mapping) else None
    if not isinstance(nodes, list):
        return None
    for node in nodes:
        if isinstance(node, Mapping) and node.get("label") == label:
            props = node.get("properties")
            if not isinstance(props, list):
                return None
            return [dict(p) for p in props if isinstance(p, Mapping) and "name" in p]
    return None


def row_properties(spec: Mapping[str, Any], label: str) -> dict[str, dict[str, Any]] | None:
    """The declarations that ride ``label``'s sync rows: every property not ``evidenceonly``.

    ``evidenceonly: true`` marks an EIE enrichment/inference/resolution input
    that is never projected onto a sync row and is not part of the row contract.
    """
    props = node_properties(spec, label)
    if props is None:
        return None
    return {p["name"]: p for p in props if p.get("evidenceonly") is not True}
