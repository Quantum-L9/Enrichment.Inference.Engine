"""Copy an Odoo capability line onto the PROCESSES element CEG stores.

Odoo keeps the polymer on ``plasticos.polymer.code`` (the stored selection
``plasticos.material.profile.polymer`` when that code is one of its keys),
the line on ``plasticos.material.profile.capability_id``
(``plasticos.capability``, one row per machine or line), and ``direction``
``in`` for material entering a buyer. CEG's ``polymer_capability_gate`` reads
``PolymerFamily.code``, ``direction`` equal to ``in``, and the PROCESSES
relationship whose identity is ``line_id``.

This module copies those stored values. It does not decide capacity,
contamination, grade, color, or which form is eligible.

L9_META:
  tier: 2
  domain: graph-sync
  authority: ceg processes gate field transport
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def prepare_processed_polymers(value: Any) -> Any:
    """Return wire elements when ``value`` is a list of Odoo material profiles.

    A list that already uses the sync keys (``code`` / ``line_id``) is unchanged.
    A profile with no polymer code or no capability line is not a PROCESSES
    line and is left out. Direction and form are copied only when Odoo stored
    them.
    """
    if not isinstance(value, list):
        return value
    if not any(isinstance(item, Mapping) and "capability_id" in item for item in value):
        return value
    elements: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, Mapping):
            element = _element(item)
            if element is not None:
                elements.append(element)
    return elements


def _element(profile: Mapping[str, Any]) -> dict[str, str] | None:
    code = _polymer_code(profile)
    line_id = _line_id(profile.get("capability_id"))
    if code is None or line_id is None:
        return None
    element = {"code": code, "line_id": line_id}
    direction = profile.get("direction")
    if isinstance(direction, str) and direction:
        element["direction"] = direction
    form = profile.get("form")
    if isinstance(form, str) and form:
        element["form"] = form
    return element


def _polymer_code(profile: Mapping[str, Any]) -> str | None:
    polymer = profile.get("polymer")
    if isinstance(polymer, str) and polymer.strip():
        return polymer.strip()
    polymer_id = profile.get("polymer_id")
    if isinstance(polymer_id, Mapping):
        code = polymer_id.get("code")
        if isinstance(code, str) and code.strip():
            return code.strip()
    return None


def _line_id(value: object) -> str | None:
    if value is None or value is False or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (list, tuple)) and value:
        return _line_id(value[0])
    if isinstance(value, Mapping):
        return _line_id(value.get("id"))
    return None
