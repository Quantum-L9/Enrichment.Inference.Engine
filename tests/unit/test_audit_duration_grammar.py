"""EIE-PR-U04: EIE never admits a duration the CEG row contract refuses.

L9_META:
  tier: 2
  domain: typed-evidence
  authority: app/engines/spec_types.py (the shared typed-declaration grammar,
    #321 "typed spec row contract shared with CEG"); CEG
    engine/sync/row_contract.py `_ISO_DURATION` at Cognitive.Engine.Graphs
    21dc906, the row check every synced `duration` value meets

The two grammars are separate implementations of one contract. EIE accepted
two forms CEG refuses, so a value could pass provider admission and the EIE row
projection and then be refused by CEG at sync:

* a time designator with no time component (``P3DT``);
* a negative duration (``-P1D``).

The cases below are the CEG verdicts for that revision. EIE stays stricter in
one place, a fraction on a unit other than seconds (``P1.5D``), which CEG
accepts: refusing more than the peer never sends a value the peer refuses.
"""

from __future__ import annotations

import pytest

from app.engines.spec_types import value_defects

pytestmark = pytest.mark.unit

DURATION = {"name": "d", "type": "duration"}


def _accepted(value: str) -> bool:
    return not value_defects(value, DURATION, "d")


@pytest.mark.parametrize(
    "value", ["P3DT", "P1Y2MT", "-P1D", "-PT1H", "P", "PT", "1D", "P1H", "PT1D"]
)
def test_a_duration_ceg_refuses_is_refused(value: str) -> None:
    assert not _accepted(value)


@pytest.mark.parametrize(
    "value", ["P3D", "P1W", "PT1H30M", "P1Y2M3DT4H5M6S", "PT0.5S", "P0D", "PT36H"]
)
def test_a_duration_ceg_accepts_is_accepted(value: str) -> None:
    assert _accepted(value)


def test_the_one_stricter_case_is_a_fraction_outside_seconds() -> None:
    """Documented asymmetry: EIE refuses, CEG accepts. Safe direction only."""
    assert not _accepted("P1.5D")
