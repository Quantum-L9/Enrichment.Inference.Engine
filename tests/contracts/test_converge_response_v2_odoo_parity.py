"""Response v2 DRAFT examples checked against the Odoo consumer's expectations.

Two independent checks over ``docs/contracts/converge/examples/response.v2.*.json``:

1. The examples obey the draft's own rules (``docs/contracts/converge/response.v2.md``
   sections 2, 3, 5 and 6).
2. A vendored copy of Odoo's v1 ``map_converge_response``
   (``tests/fixtures/odoo_parity/``) reads them to the hand-written expected
   result and never sees an ``unknown`` or ``conflicting`` field as a value.

This is fixture parity. It is not evidence that a running Odoo consumes v2.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.models.schemas import EnrichResponse

_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = _ROOT / "docs" / "contracts" / "converge" / "examples"
_PARITY = _ROOT / "tests" / "fixtures" / "odoo_parity"

# Declared, not discovered: a missing or extra file must fail, never shrink the check.
_NAMES = ("supplier-material", "supplier-facility", "buyer-material", "buyer-facility", "failed")
_VERSION = "converge.response/2.0.0-draft"
_VALUE_STATES = {"observed", "inferred", "external_claim"}
_NO_VALUE_STATES = {"unknown", "conflicting"}
_UNKNOWN_REASONS = {"no_evidence", "source_unavailable", "budget_exhausted", "not_attempted"}
_V2_KEYS = ("contract_version", "field_evidence", "proposals")


def _load_mapper() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "odoo_converge_mapper", _PARITY / "odoo_converge_mapper.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MAPPER = _load_mapper()


def _json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _example(name: str) -> dict[str, Any]:
    return _json(_EXAMPLES / f"response.v2.{name}.json")


def _expected(name: str) -> dict[str, Any]:
    return _json(_PARITY / "expected" / f"{name}.json")


def _check_value_shape(holder: dict[str, Any], where: str) -> None:
    assert holder["unit_status"] in {"stated", "not_applicable", "unknown"}, where
    assert ("unit" in holder) == (holder["unit_status"] == "stated"), where


def _check_entry(name: str, entry: dict[str, Any]) -> None:
    state = entry["state"]
    assert state in _VALUE_STATES | _NO_VALUE_STATES, name
    assert isinstance(entry["admitted"], bool), name
    assert ("value" in entry) == (state in _VALUE_STATES), name
    if "value" in entry:
        _check_value_shape(entry, name)
    else:
        assert "unit" not in entry and "unit_status" not in entry, name
        assert entry["admitted"] is False and entry["confidence"] is None, name
    confidence = entry["confidence"]
    assert confidence is None or (isinstance(confidence, float) and 0.0 <= confidence <= 1.0), name
    assert (entry["derivation"] is not None) == (state == "inferred"), name
    assert (entry["conflict"] is not None) == (state == "conflicting"), name
    assert (entry["unknown_reason"] is not None) == (state == "unknown"), name
    if state == "unknown":
        assert entry["unknown_reason"] in _UNKNOWN_REASONS, name
    if state in {"observed", "external_claim"}:
        assert entry["sources"], name
    if state == "observed":
        assert all(s["source_type"].startswith("request_") for s in entry["sources"]), name
    for source in entry["sources"]:
        assert source["source_ref"] and source["method"] and "independent" in source, name
    if state == "inferred":
        assert entry["derivation"]["rule_or_model_version"], name
    if state == "conflicting":
        candidates = entry["conflict"]["candidates"]
        values = [json.dumps(c["value"], sort_keys=True) for c in candidates]
        assert len(candidates) >= 2 and len(set(values)) == len(values), name
        assert entry["conflict"]["resolution"] is None, name
        for candidate in candidates:
            _check_value_shape(candidate, name)
            assert candidate["sources"], name
    assert entry["identity"]["subject_ref"] and entry["identity"]["subject_type"], name


def test_every_declared_example_and_expectation_exists_and_nothing_else() -> None:
    assert sorted(p.name for p in _EXAMPLES.glob("*.json")) == sorted(
        f"response.v2.{n}.json" for n in _NAMES
    )
    assert sorted(p.name for p in (_PARITY / "expected").glob("*.json")) == sorted(
        f"{n}.json" for n in _NAMES
    )


@pytest.mark.parametrize("name", _NAMES)
def test_example_obeys_the_draft_rules(name: str) -> None:
    payload = _example(name)
    evidence = payload["field_evidence"]
    assert payload["contract_version"] == _VERSION
    assert evidence, "an example with no field evidence checks nothing"
    for field, entry in evidence.items():
        _check_entry(f"{name}:{field}", entry)
        if entry["state"] == "inferred":
            assert set(entry["derivation"]["inputs"]) <= set(evidence), field
            assert set(entry["derivation"]["kb_fragment_ids"]) <= set(payload["kb_fragment_ids"]), (
                field
            )
    admitted = {f: e["value"] for f, e in evidence.items() if e["admitted"]}
    assert payload["fields"] == admitted
    failed = payload["state"] != "completed" or bool(payload["failure_reason"])
    if failed:
        assert payload["fields"] == {} and payload["proposals"] == []
    for proposal in payload["proposals"]:
        assert proposal["review"]["required"] is True and proposal["review"]["reasons"]
        assert set(proposal["fields"]) <= set(evidence)
        assert proposal["admitted_fields"] == [
            f for f in proposal["fields"] if evidence[f]["admitted"]
        ]
        assert all(evidence[f]["identity"] == proposal["identity"] for f in proposal["fields"])


def test_examples_cover_every_state_role_and_an_unadmitted_claim() -> None:
    entries = [e for n in _NAMES for e in _example(n)["field_evidence"].values()]
    assert {e["state"] for e in entries} == _VALUE_STATES | _NO_VALUE_STATES
    assert {e["identity"]["role"] for e in entries} == {"supplier", "buyer"}
    assert any(e["state"] == "external_claim" and not e["admitted"] for e in entries)
    assert not any(e["state"] == "observed" and e["confidence"] is not None for e in entries)


@pytest.mark.parametrize("name", _NAMES)
def test_odoo_v1_mapper_reads_the_example_as_expected(name: str) -> None:
    payload, expected = _example(name), _expected(name)
    mapped = _MAPPER.map_converge_response(payload)
    assert mapped["status"] == expected["status"]
    assert mapped["final_fields"] == expected["final_fields"]
    assert _MAPPER.partner_writeback_from_converge(mapped) == expected["partner_writeback"]
    assert mapped["total_cost_usd"] is None and mapped["writeback_applied"] is None
    assert mapped["raw"] is payload and mapped["raw"]["field_evidence"] == payload["field_evidence"]
    for key in _MAPPER.CARRIED_THROUGH:
        assert mapped[key] == payload[key], key
    for key in _MAPPER.CARRIED_AS_LIST:
        assert mapped[key] == payload[key], key


@pytest.mark.parametrize("name", _NAMES)
def test_unknown_conflicting_and_unadmitted_fields_never_reach_odoo_as_values(name: str) -> None:
    payload, expected = _example(name), _expected(name)
    withheld = {f for f, e in payload["field_evidence"].items() if not e["admitted"]}
    assert withheld == set(expected["never_as_value"])
    mapped = _MAPPER.map_converge_response(payload)
    assert withheld.isdisjoint(mapped["final_fields"])
    assert withheld.isdisjoint(_MAPPER.partner_writeback_from_converge(mapped))


@pytest.mark.parametrize("name", _NAMES)
def test_v2_keys_do_not_change_what_a_v1_consumer_reads(name: str) -> None:
    payload = _example(name)
    v1_only = {k: v for k, v in payload.items() if k not in _V2_KEYS}
    with_v2, without_v2 = (
        _MAPPER.map_converge_response(payload),
        _MAPPER.map_converge_response(v1_only),
    )
    with_v2.pop("raw")
    without_v2.pop("raw")
    assert with_v2 == without_v2
    model = EnrichResponse.model_validate(payload)
    assert model.fields == payload["fields"] and model.state == payload["state"]
    assert not set(_V2_KEYS) & set(EnrichResponse.model_fields), (
        "v2 is a draft: the v1 model must not carry it yet"
    )


_CASES = _json(_PARITY / "mapper_cases.json")["cases"]


def test_vendored_mapper_cases_are_present() -> None:
    assert len(_CASES) == 12


@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_vendored_mapper_matches_odoos_own_test_expectations(case: dict[str, Any]) -> None:
    mapped = _MAPPER.map_converge_response(case["payload"])
    for key, value in case["expect"].items():
        assert mapped[key] == value, key
    if "expect_partner_writeback" in case:
        assert _MAPPER.partner_writeback_from_converge(mapped) == case["expect_partner_writeback"]
