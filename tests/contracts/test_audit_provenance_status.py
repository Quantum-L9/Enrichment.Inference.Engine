"""EIE-D-005: the native spec's sync-provenance status matches the code and the packs.

L9_META:
  tier: 2
  domain: graph-sync
  authority: docs/contracts/EIE_NATIVE_DOMAIN_SPEC.md "Sync provenance";
    docs/contracts/data/graph_sync_intents.md "Field set and provenance"

The status block read "target contract ... takes effect with the implementation
that follows (campaign PR-2). Until then every graph-sync row carries admitted
values only". PR-2 merged (#420, #421): `app/engines/sync_field_set.py` selects
observed and enriched values per field and labels them whenever the endpoint
declares the provenance property. The status must say what is true now: the
code implements both modes, and which mode a field syncs in is chosen by the
declarations a published pack carries -- which, for the packs in this
repository, still declare no provenance property, so they sync in legacy mode.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.engines import sync_field_set

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
SPEC_DOC = REPO / "docs/contracts/EIE_NATIVE_DOMAIN_SPEC.md"


def _status_block() -> str:
    text = SPEC_DOC.read_text(encoding="utf-8")
    section = text.split("## Sync provenance", 1)[1].split("\n## ", 1)[0]
    match = re.search(r"((?:^> .*\n?)+)", section, flags=re.MULTILINE)
    assert match, "the Sync provenance section carries a status block"
    return " ".join(line.removeprefix("> ").strip() for line in match.group(1).splitlines())


def test_status_does_not_describe_implemented_rules_as_future_only() -> None:
    status = _status_block()
    for stale in ("target contract", "take effect with the implementation", "Until then"):
        assert stale not in status, f"stale status wording: {stale!r}"


def test_status_names_the_implementation_and_both_modes() -> None:
    status = _status_block()
    assert "app/engines/sync_field_set.py" in status
    assert "legacy" in status.lower() and "full" in status.lower()


def test_the_code_implements_the_provenance_convention_the_doc_defines() -> None:
    """The names the doc defines are the ones the implementation uses."""
    assert sync_field_set.ROW_PROVENANCE_SUFFIX == "_provenance"
    assert sync_field_set.ITEM_PROVENANCE == "provenance"
    assert sync_field_set.provenance_property("capacity") == "capacity_provenance"


@pytest.mark.parametrize(
    "pack", ["domains/plasticos/spec.yaml", "tests/fixtures/ceg/plasticos.spec.yaml"]
)
def test_status_matches_the_packs_in_this_repository(pack: str) -> None:
    """A pack that declares no provenance property syncs in legacy mode; the status says so."""
    text = (REPO / pack).read_text(encoding="utf-8")
    yaml.safe_load(text)  # a real, parseable pack
    declares_provenance = "_provenance" in text or re.search(r"name:\s*provenance\b", text)
    status = _status_block()
    if not declares_provenance:
        assert "declare no provenance property" in status, (
            f"{pack} declares no provenance property, so it syncs in legacy mode; "
            "the status must say that the in-repository packs are legacy today"
        )
