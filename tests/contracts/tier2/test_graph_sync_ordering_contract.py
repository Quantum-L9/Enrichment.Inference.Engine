"""Tier 2: the graph-sync ordering guarantee matches its data contract.

Contract: docs/contracts/data/graph_sync_intents.md rule 5 (*Version order and
the max-apply window*) and G4. At most one version of a subject is in flight;
a newer version waits out the max-apply window of an uncertain older one. The
documented window is the store's, and it covers every bound EIE itself owns
for one request: the attempts of one dispatch and the lease of one claim.
The behaviour is exercised on PostgreSQL in
tests/integration/test_graph_sync_late_write_pg.py.

L9_META:
  tier: 2
  domain: graph-sync
  authority: CTO REVIEW of #419 (late-write reconciliation)
  pr_class: tier2_test
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from app.engines import packet_router
from app.services import pg_store
from app.services.side_effect_coordinator import GRAPH_LEASE_SECONDS

REPO = Path(__file__).resolve().parents[3]
DOC = REPO / "docs" / "contracts" / "data" / "graph_sync_intents.md"


def _rule5() -> str:
    doc = DOC.read_text(encoding="utf-8")
    start = doc.index("5. **Version order and the max-apply window.**")
    return doc[start : doc.index("6. **Bounded retries.**", start)]


def test_the_documented_window_is_the_store_window() -> None:
    rule = " ".join(_rule5().split())
    assert "At most one version of a subject is in flight." in rule
    match = re.search(r"`GRAPH_MAX_APPLY_SECONDS`, (\d+) s", rule)
    assert match is not None
    assert float(match.group(1)) == pg_store.GRAPH_MAX_APPLY_SECONDS


def test_every_claim_applies_the_window_by_default() -> None:
    parameter = inspect.signature(pg_store.claim_graph_intents).parameters["max_apply_seconds"]
    assert parameter.default == pg_store.GRAPH_MAX_APPLY_SECONDS


def test_the_window_covers_the_bounds_eie_owns_for_one_request() -> None:
    attempts = packet_router._MAX_ATTEMPTS
    dispatch = attempts * packet_router._ROUTE_TIMEOUT + (
        (attempts - 1) * packet_router._BACKOFF_BASE_SECONDS * 2 ** (attempts - 1)
    )
    assert dispatch < pg_store.GRAPH_MAX_APPLY_SECONDS
    assert pg_store.GRAPH_MAX_APPLY_SECONDS > GRAPH_LEASE_SECONDS


def test_g4_never_suppresses_against_an_uncertain_send() -> None:
    sent_unacknowledged = ("failed_terminal", 2, {"band": "B1"})
    acknowledged = ("acknowledged", 1, {"band": "B1"})
    assert pg_store.settled_graph_fields([sent_unacknowledged, acknowledged]) == {}
    doc = " ".join(DOC.read_text(encoding="utf-8").split())
    assert "rule 5 sends that resend only after the uncertain intent's max-apply window" in doc
