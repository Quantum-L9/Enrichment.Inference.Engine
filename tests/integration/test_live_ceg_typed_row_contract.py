"""eie-ceg-sync/v2 against a LIVE Cognitive.Engine.Graphs checkout (CEG_REPO).

The unit test proves the contract against the vendored snapshot
(tests/fixtures/ceg). This one reads the plasticos spec of the CEG checkout the
operator or CI supplies, so a CEG change that drifts the row contract is seen
against the commit actually checked out. Skipped when CEG_REPO is not set, like
tests/integration/test_cross_engine_native_release.py.

L9_META:
  tier: 2
  domain: native-domain
  authority: owner decision 2026-11-11 (typed spec contract)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from app.engines.domain_spec_compatibility import check_compatibility

ROOT = Path(__file__).resolve().parents[2]
CEG_ROOT = os.environ.get("CEG_REPO")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not CEG_ROOT, reason="no live CEG checkout supplied (CEG_REPO)"),
]


def test_eie_row_contract_matches_the_live_ceg_spec() -> None:
    eie = yaml.safe_load((ROOT / "domains/plasticos/spec.yaml").read_text())
    ceg = yaml.safe_load((Path(str(CEG_ROOT)) / "domains/plasticos/spec.yaml").read_text())
    result = check_compatibility(eie, ceg)
    assert result["verdict"] == "pass", result["defects"]
