"""The converge graph field set: legacy and full modes, G1/G2/G4/G5 (operator ruling 2026-11-24).

Contract: docs/contracts/data/graph_sync_intents.md *Field set and provenance*;
convention: docs/contracts/EIE_NATIVE_DOMAIN_SPEC.md *Sync provenance*. Shapes:
the gate_loop synthneutral cartridge (childsync ``handled_kinds`` with item
``provenance``, ``band`` with ``band_provenance``) and a second, in-memory
synthetic shape. No PlasticOS fixture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.engines import sync_field_set as fs
from app.engines.handlers import graph_origin_fields, observed_values
from app.engines.packet_router import project_graph_sync_row, resolve_graph_sync_contract
from app.services.pg_store import settled_graph_fields

pytestmark = pytest.mark.unit

SYNTH = (
    Path(__file__).resolve().parents[2] / "tests/e2e/gate_loop/cartridges/synthneutral/spec.yaml"
)
KINDS = [{"code": "K1", "slot_id": "s-1"}]


def _contract() -> Any:
    spec = yaml.safe_load(SYNTH.read_bytes())
    contract = resolve_graph_sync_contract(spec, node_label="Station")
    assert contract is not None and contract.row_schema is not None
    return contract


def _select(**kwargs: Any) -> fs.Selection:
    contract = _contract()
    kwargs.setdefault("graph_origin", ())
    return fs.select(row_schema=contract.row_schema, id_property=contract.id_property, **kwargs)


# A second, unrelated shape: a scalar and a single childsync map, both full.
GAUGE_SCHEMA: dict[str, Any] = {
    "gauge_id": {"name": "gauge_id", "type": "string"},
    "reading": {"name": "reading", "type": "float"},
    "reading_provenance": {"name": "reading_provenance", "type": "enum"},
    "site": {
        "name": "site",
        "type": "map",
        "properties": [{"name": "ref", "type": "string"}, {"name": "provenance", "type": "enum"}],
    },
    "unit": {"name": "unit", "type": "string"},  # legacy: no provenance declared
}


def test_modes_come_only_from_declarations() -> None:
    contract = _contract()
    full = {n for n in contract.row_schema if fs.full_mode(n, contract.row_schema)}
    assert full == {"band", "handled_kinds"}
    assert {n for n in GAUGE_SCHEMA if fs.full_mode(n, GAUGE_SCHEMA)} == {"reading", "site"}
    assert fs.provenance_properties(GAUGE_SCHEMA) == {"reading_provenance"}


def test_legacy_fields_are_admitted_fields_byte_identical() -> None:
    admitted = {"title": "T", "max_load": 40.0}
    selection = _select(admitted=admitted, observed={"title": "OBS", "region": "R"})
    assert selection.values == admitted and selection.provenance == {}
    # With no row schema at all, the selection is admitted_fields itself.
    assert fs.select(admitted=admitted, observed={"x": 1}, graph_origin=(), row_schema=None) == (
        fs.Selection(values=admitted)
    )


def test_an_observed_only_childsync_collection_syncs_labelled_observed() -> None:
    selection = _select(admitted={}, observed={"handled_kinds": KINDS})
    fields = fs.label(selection.values, selection.provenance, _contract().row_schema)
    assert fields == {"handled_kinds": [{**KINDS[0], "provenance": "observed"}]}
    row = project_graph_sync_row(entity_id="st-1", fields=fields, contract=_contract())
    assert row.defects == () and row.row is not None
    assert row.row["handled_kinds"] == [{**KINDS[0], "provenance": "observed"}]


def test_enriched_wins_over_observed_and_is_labelled_enriched() -> None:
    selection = _select(admitted={"band": "B2"}, observed={"band": "B9"})
    fields = fs.label(selection.values, selection.provenance, _contract().row_schema)
    assert fields == {"band": "B2", "band_provenance": "enriched"}


def test_a_graph_origin_value_is_never_sent_and_the_observation_falls_back() -> None:
    selection = _select(
        admitted={"band": "FROM-GRAPH"}, observed={"band": "B9"}, graph_origin={"band"}
    )
    assert selection.values == {"band": "B9"} and selection.provenance == {"band": "observed"}
    alone = _select(admitted={"band": "FROM-GRAPH"}, observed={}, graph_origin={"band"})
    assert alone.values == {} and alone.provenance == {}


def test_conflicting_and_null_observations_send_nothing() -> None:
    assert _select(admitted={}, observed={"band": "B9"}, conflicting={"band"}).values == {}
    assert _select(admitted={}, observed={"band": None}).values == {}


def test_an_observation_never_reaches_a_legacy_field() -> None:
    selection = fs.select(
        admitted={},
        observed={"unit": "kg", "reading": 1.5},
        graph_origin=(),
        row_schema=GAUGE_SCHEMA,
    )
    assert selection.values == {"reading": 1.5}
    assert "unit" not in selection.values


def test_a_single_map_and_a_scalar_are_labelled_by_the_convention() -> None:
    selection = fs.select(
        admitted={"site": {"ref": "S1"}},
        observed={"reading": 2.0},
        graph_origin=(),
        row_schema=GAUGE_SCHEMA,
        id_property="gauge_id",
    )
    fields = fs.label(selection.values, selection.provenance, GAUGE_SCHEMA)
    assert fields == {
        "site": {"ref": "S1", "provenance": "enriched"},
        "reading": 2.0,
        "reading_provenance": "observed",
    }


def test_echo_guard_drops_only_a_settled_identical_full_mode_field() -> None:
    fields = {"band": "B2", "band_provenance": "enriched", "title": "T"}
    provenance = {"band": "enriched"}
    settled = {"band": {"band": "B2", "band_provenance": "enriched"}}
    assert fs.apply_echo_guard(fields, provenance, settled) == {"title": "T"}
    changed = {"band": {"band": "B1", "band_provenance": "enriched"}}
    assert fs.apply_echo_guard(fields, provenance, changed) == fields
    relabelled = {"band": {"band": "B2", "band_provenance": "observed"}}
    assert fs.apply_echo_guard(fields, provenance, relabelled) == fields
    # A legacy field is never suppressed, whatever the history holds.
    assert fs.apply_echo_guard(fields, provenance, {"title": {"title": "T"}}) == fields


def test_settled_requires_every_sent_intent_carrying_the_field_acknowledged() -> None:
    acked = ("acknowledged", 1, {"band": "B2", "band_provenance": "enriched"})
    assert settled_graph_fields([acked]) == {"band": {"band": "B2", "band_provenance": "enriched"}}
    sent_failed = ("failed_terminal", 3, {"band": "B1", "band_provenance": "enriched"})
    assert settled_graph_fields([sent_failed, acked]) == {}  # its late arrival is possible
    assert settled_graph_fields([acked, sent_failed]) == {}  # newer failure: uncertain
    assert settled_graph_fields([acked, ("pending", 0, {"band": "B3"})]) == {}  # outstanding
    never_sent = ("superseded", 0, {"band": "B0"})
    assert settled_graph_fields([never_sent, acked])["band"]["band"] == "B2"
    # Fields are independent: an uncertain field does not unsettle another one.
    mixed = [("failed_terminal", 1, {"title": "X"}), acked]
    assert set(settled_graph_fields(mixed)) == {"band"}


def test_graph_origin_is_the_last_writer_by_pass() -> None:
    result = {
        "inferences": [
            {"graph_seeded": ["band", "region"], "enriched": [], "inferred": []},
            {"graph_seeded": [], "enriched": ["band"], "inferred": []},
        ]
    }
    assert graph_origin_fields(result) == {"region"}


def test_observed_values_take_v2_observations_and_drop_conflicts() -> None:
    result = {
        "field_evidence": {
            "band": {"state": "observed", "value": "B7"},
            "region": {"state": "conflicting"},
            "title": {"state": "unknown"},
        }
    }
    observed, conflicting = observed_values({"band": "B1", "region": "R", "note": None}, result)
    assert observed == {"band": "B7"}
    assert conflicting == {"region"}
