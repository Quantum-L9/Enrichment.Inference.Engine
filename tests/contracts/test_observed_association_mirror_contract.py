"""
Observed Association Mirror Contract
Source: app/engines/observed_association_contract.py <-> Cognitive.Engine.Graphs
        engine/config/schema.py (ObservedAssociationSourceSpec, refnamespace).
Markers: unit

EIE resolves ``observedfrom`` / ``refnamespace`` from its mirror of the tenant
spec and must refuse exactly what CEG refuses at load, or a mirror entry CEG would
never load could still map an association (QM.POC0.TENANT_ASSOCIATION_MAPPING.004).
The CEG grammars are pinned here as of Cognitive.Engine.Graphs#387, and EIE's
resolver is checked against them on a fixed corpus.

When ``L9_SEAM_CEG_ROOT`` names a Cognitive.Engine.Graphs checkout (the synthetic
E2E environment sets it), the pins are also compared with CEG's live schema
source, so a CEG grammar change fails here instead of drifting silently. Without
it the live comparison is skipped: CI has only this repository, and the pinned
comparison above still runs.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# Cognitive.Engine.Graphs engine/config/schema.py (#387).
CEG_PATTERNS = {
    "PROVIDER_TOKEN_PATTERN": r"^[a-z][a-z0-9_-]{1,31}$",
    "PROVIDER_OBJECT_TYPE_PATTERN": r"^(?:[a-z][a-z0-9_]{0,63}|\d+-\d+)$",
    "PROVIDER_ASSOCIATION_CATEGORY_PATTERN": r"^[A-Z][A-Z0-9_]{0,63}$",
    "REF_NAMESPACE_PATTERN": r"^[a-z0-9_.-]{1,64}$",
}
CEG_ENTRY_KEYS = {
    "provider",
    "fromobjecttype",
    "toobjecttype",
    "associationcategory",
    "associationtypeid",
}

CORPUS = [
    "",
    "x",
    "hubspot",
    "HubSpot",
    "hub-spot_2",
    "a" * 32,
    "a" * 33,
    "companies",
    "2-1234",
    "0-3",
    "2-",
    "companies/1",
    "../deals",
    "HUBSPOT_DEFINED",
    "hubspot_defined",
    "_X",
    "orbit.depot",
    "orbit:depot",
    "orbit depot",
    "a" * 64,
    "a" * 65,
]


def _eie() -> dict[str, re.Pattern[str]]:
    from app.engines import observed_association_contract as contract

    return {
        "PROVIDER_TOKEN_PATTERN": contract.PROVIDER_TOKEN,
        "PROVIDER_OBJECT_TYPE_PATTERN": contract.PROVIDER_OBJECT_TYPE,
        "PROVIDER_ASSOCIATION_CATEGORY_PATTERN": contract.PROVIDER_ASSOCIATION_CATEGORY,
        "REF_NAMESPACE_PATTERN": contract.REF_NAMESPACE,
    }


@pytest.mark.parametrize("name", sorted(CEG_PATTERNS))
def test_eie_refuses_exactly_what_the_ceg_grammar_refuses(name: str) -> None:
    eie = _eie()[name]
    ceg = re.compile(CEG_PATTERNS[name])
    for value in CORPUS:
        assert (eie.fullmatch(value) is not None) == (ceg.match(value) is not None), (name, value)


def test_the_entry_shape_matches_the_ceg_model() -> None:
    from app.engines import observed_association_contract as contract

    assert set(contract._ENTRY_KEYS) == CEG_ENTRY_KEYS


def _ceg_schema() -> Path | None:
    # Explicit only: a sibling clone may sit on a branch that predates the mapping.
    raw = os.environ.get("L9_SEAM_CEG_ROOT", "").strip()
    if not raw:
        return None
    schema = Path(raw) / "engine" / "config" / "schema.py"
    return schema if schema.exists() else None


def test_the_pins_match_the_live_ceg_schema_when_a_checkout_is_present() -> None:
    schema = _ceg_schema()
    if schema is None:
        pytest.skip(
            "no Cognitive.Engine.Graphs checkout (L9_SEAM_CEG_ROOT); pinned comparison still ran"
        )
    source = schema.read_text(encoding="utf-8")
    for name, pattern in CEG_PATTERNS.items():
        match = re.search(rf'^{name} = r"(.+)"$', source, flags=re.MULTILINE)
        assert match is not None, f"{name} not found in {schema}"
        assert match.group(1) == pattern, name
    for key in CEG_ENTRY_KEYS:
        assert re.search(rf"^    {key}: ", source, flags=re.MULTILINE), key
