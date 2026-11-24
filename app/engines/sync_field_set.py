"""The field set one converge sends to CEG, with per-field provenance.

Contract: ``docs/contracts/data/graph_sync_intents.md`` *Field set and
provenance* (operator ruling 2026-11-24) and the *Sync provenance* convention of
``docs/contracts/EIE_NATIVE_DOMAIN_SPEC.md``. Keyed only on the endpoint's
declared row properties (``row_schema``) -- no field name, node label or
cartridge is known here (G6).

Per declared value field ``F`` the endpoint selects a mode:

* **legacy** -- ``F``'s provenance property is not declared: the admitted value
  is sent exactly as before, unlabelled (unlabelled means enriched).
* **full** -- ``F_provenance`` (row) or the item property ``provenance``
  (childsync ``map`` / ``list<map>``) is declared: graph-origin values are
  excluded, an admitted value is sent as ``enriched`` and wins, else the
  consumer's observed value is sent as ``observed`` (G1, G2, G5).

The caller selects (:func:`select`), shapes the selected values with its
existing single writer, then labels (:func:`label`) and applies the echo guard
(:func:`apply_echo_guard`, G4). An observed value never reaches a legacy field.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

OBSERVED = "observed"
ENRICHED = "enriched"
ROW_PROVENANCE_SUFFIX = "_provenance"
ITEM_PROVENANCE = "provenance"
# Transport metadata (graph_sync_intents.md rule 6): set at send time from the
# intent's version, never a value field of the converge.
SYNC_VERSION = "sync_version"


def provenance_property(name: str) -> str:
    """The companion row property that carries ``name``'s provenance."""
    return f"{name}{ROW_PROVENANCE_SUFFIX}"


def _item_properties(declaration: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    """The item properties of a ``map`` or ``list<map>`` declaration, else None."""
    if declaration.get("type") == "map":
        props = declaration.get("properties")
    elif declaration.get("type") == "list":
        items = declaration.get("items")
        if not isinstance(items, Mapping) or items.get("type") != "map":
            return None
        props = items.get("properties")
    else:
        return None
    return [p for p in props if isinstance(p, Mapping)] if isinstance(props, list) else []


def _declares_item_provenance(declaration: Mapping[str, Any]) -> bool:
    props = _item_properties(declaration)
    return props is not None and any(p.get("name") == ITEM_PROVENANCE for p in props)


def provenance_properties(row_schema: Mapping[str, Any]) -> frozenset[str]:
    """Declared row properties that are provenance metadata, not value fields."""
    return frozenset(
        name
        for name in row_schema
        if name.endswith(ROW_PROVENANCE_SUFFIX)
        and name[: -len(ROW_PROVENANCE_SUFFIX)] in row_schema
    )


def full_mode(name: str, row_schema: Mapping[str, Any]) -> bool:
    """True when value field ``name`` declares its provenance property."""
    declaration = row_schema.get(name)
    if not isinstance(declaration, Mapping):
        return False
    if _item_properties(declaration) is not None:
        return _declares_item_provenance(declaration)
    return provenance_property(name) in row_schema


@dataclass(frozen=True)
class Selection:
    """Selected values and, for full-mode fields only, their provenance."""

    values: dict[str, Any]
    provenance: dict[str, str] = field(default_factory=dict)


def select(
    *,
    admitted: Mapping[str, Any],
    observed: Mapping[str, Any],
    graph_origin: Iterable[str],
    conflicting: Iterable[str] = (),
    row_schema: Mapping[str, Any] | None,
    id_property: str | None = None,
) -> Selection:
    """Choose each field's value and provenance (G1, G2, G5); legacy is unchanged.

    ``admitted`` is ``handlers.admitted_fields`` -- exactly what synced before
    the ruling. With no ``row_schema`` every field is legacy and the selection
    is ``admitted`` itself.
    """
    if not row_schema:
        return Selection(values=dict(admitted))
    if SYNC_VERSION in row_schema:
        admitted = {k: v for k, v in admitted.items() if k != SYNC_VERSION}
    from_graph = frozenset(graph_origin)
    conflicts = frozenset(conflicting)
    meta = provenance_properties(row_schema)
    values: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    for name, value in admitted.items():
        if name in meta or full_mode(name, row_schema):
            continue
        values[name] = value  # legacy: byte-identical to the pre-ruling sync
    for name in row_schema:
        if name in meta or name in (id_property, SYNC_VERSION) or not full_mode(name, row_schema):
            continue
        if name in admitted and name not in from_graph and admitted[name] is not None:
            values[name], provenance[name] = admitted[name], ENRICHED
        elif name not in conflicts and observed.get(name) is not None:
            values[name], provenance[name] = observed[name], OBSERVED
    return Selection(values=values, provenance=provenance)


def _labelled_item(item: Any, provenance: str) -> Any:
    return {**item, ITEM_PROVENANCE: provenance} if isinstance(item, Mapping) else item


def label(
    values: Mapping[str, Any], provenance: Mapping[str, str], row_schema: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Attach each full-mode field's provenance by the declared convention (G1)."""
    fields = dict(values)
    for name, source in provenance.items():
        if name not in fields or row_schema is None:
            continue
        declaration = row_schema.get(name, {})
        if _item_properties(declaration) is not None:
            value = fields[name]
            fields[name] = (
                [_labelled_item(item, source) for item in value]
                if isinstance(value, list)
                else _labelled_item(value, source)
            )
        else:
            fields[provenance_property(name)] = source
    return fields


def apply_echo_guard(
    fields: Mapping[str, Any],
    provenance: Mapping[str, str],
    settled: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Drop full-mode fields the graph provably holds already (G4).

    ``settled`` maps a field to the labelled values (``F`` and, for a row field,
    ``F_provenance``) the store may suppress against -- only fields whose every
    sent intent ended acknowledged (``pg_store.graph_sync_settled_fields``).
    Legacy fields are never suppressed.
    """
    kept = dict(fields)
    for name in provenance:
        known = settled.get(name)
        if known is None or name not in kept:
            continue
        companion = provenance_property(name)
        current = {name: kept[name]}
        if companion in kept:
            current[companion] = kept[companion]
        if dict(known) == current:
            kept.pop(name, None)
            kept.pop(companion, None)
    return kept
