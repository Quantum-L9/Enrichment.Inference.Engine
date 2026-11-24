"""Tier 2: the G4 echo guard in the store matches its data contract.

Contract: docs/contracts/data/graph_sync_intents.md *Field set and provenance*,
G4. A field value is suppressed only against history in which the latest
intent carrying it is acknowledged with the same labelled value and every
sent intent carrying it ended acknowledged; never-sent intents do not count.
Exercised on PostgreSQL in tests/integration/test_graph_sync_echo_guard_pg.py.

L9_META:
  tier: 2
  domain: graph-sync
  authority: CTO ASSIGN #351 rule revision (operator ruling 2026-11-24), PR-2
  pr_class: tier2_test
"""

from __future__ import annotations

from pathlib import Path

from app.services.pg_store import settled_graph_fields

REPO = Path(__file__).resolve().parents[3]
DOC = REPO / "docs" / "contracts" / "data" / "graph_sync_intents.md"
LABELLED = {"band": "B2", "band_provenance": "enriched"}


def _doc() -> str:
    return " ".join(DOC.read_text(encoding="utf-8").split())


def test_the_contract_states_both_suppression_conditions() -> None:
    doc = _doc()
    assert "most recent intent carrying `F` is `acknowledged` with the same value and" in doc
    assert (
        "every intent of the subject carrying `F` that was ever sent (claimed at least once)"
        " ended `acknowledged`" in doc
    )


def test_only_a_fully_acknowledged_sent_history_settles_a_field() -> None:
    acked = ("acknowledged", 1, LABELLED)
    assert settled_graph_fields([acked]) == {"band": LABELLED}
    for unsettled in ("failed_terminal", "superseded", "pending", "dispatching"):
        assert settled_graph_fields([acked, (unsettled, 1, {"band": "B3"})]) == {}


def test_an_intent_never_sent_does_not_unsettle_a_field() -> None:
    assert settled_graph_fields([("superseded", 0, {"band": "B0"}), ("acknowledged", 1, LABELLED)])
