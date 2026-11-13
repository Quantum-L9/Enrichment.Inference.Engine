"""Request v2.1 `observations`: parsing and evidence semantics (Campaign B, B1).

Observations are evidence about one declared field. They are validated with the
request, kept out of the body the loop runs on, and stated in response v2 terms
as `observed` / `request_observation`: never admitted, never scored by EIE,
never generalized, never reconciled. All records here are synthetic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.engines.converge_request_v2 import (
    REQUEST_CONTRACT_V2,
    ConvergeRequestRefusedError,
    resolve_converge_request_v2,
)
from app.engines.convergence.observation_evidence import observation_evidence
from app.engines.domain_yaml_reader import DomainYamlReader
from app.models.schemas import EnrichRequest

pytestmark = [pytest.mark.unit, pytest.mark.enforcement, pytest.mark.provenance]

DOMAINS = Path(__file__).resolve().parents[3] / "domains"
SPEC = yaml.safe_load((DOMAINS / "plasticos/spec.yaml").read_bytes())
IDENTITY = {"subject_ref": "res.partner:55", "subject_type": None, "role": None}
TOLERANCE, POLYMERS = "contamination_tolerance", "processed_polymers"


def _obs(field: str = TOLERANCE, value: Any = 0.02, **extra: Any) -> dict[str, Any]:
    return {
        "field": field,
        "value": value,
        "source_kind": "transaction",
        "source_ref": "sale.order:1",
        **extra,
    }


def _body(observations: Any = ..., **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "contract": REQUEST_CONTRACT_V2,
        "cartridge": {"id": "plasticos", "version": SPEC["version"]},
        "node": "Facility",
        "targets": [{"field": TOLERANCE}],
        "entity": {"id": "res.partner:55", "name": "Synthetic Works"},
        "object_type": "res.partner",
        "objective": "Enrich the facility profile",
        **overrides,
    }
    if observations is not ...:
        body["observations"] = observations
    return body


def _resolve(body: dict[str, Any]) -> Any:
    return resolve_converge_request_v2("plasticos", body, DomainYamlReader(DOMAINS))


# ── Parsing in the v2 request path ─────────────────────────────────────────────


def test_observations_resolve_beside_the_body_and_never_inside_it() -> None:
    full = _obs(unit="fraction", observed_at="2026-11-01T10:00:00Z", confidence=1)
    other = _obs(POLYMERS, ["HDPE"], source_ref="sale.order:2")  # declared, though not a target
    resolved = _resolve(_body([full, other]))
    assert resolved.observations == ({**full, "confidence": 1.0}, other)
    assert "observations" not in resolved.body
    # The loop runs on the body: an observation is not an entity value there.
    request = EnrichRequest.model_validate(resolved.body)
    assert TOLERANCE not in request.entity and POLYMERS not in request.entity
    assert set(request.schema_ or {}) == {TOLERANCE}  # observing a field does not target it


def test_absent_and_empty_observations_are_no_observations() -> None:
    assert _resolve(_body()).observations == ()
    assert _resolve(_body([])).observations == ()


@pytest.mark.parametrize(
    ("code", "observations"),
    [
        ("invalid_request", {"field": TOLERANCE}),
        ("invalid_request", ["not an object"]),
        ("invalid_request", [{"field": TOLERANCE, "value": 1, "source_kind": "t"}]),
        ("invalid_request", [_obs(note="unknown member")]),
        ("invalid_request", [_obs(source_ref="")]),
        ("invalid_request", [_obs(source_kind=7)]),
        ("invalid_request", [_obs(unit="")]),
        ("invalid_request", [_obs(value=None)]),
        ("invalid_request", [_obs(confidence=1.2)]),
        ("invalid_request", [_obs(confidence=True)]),
        ("invalid_request", [_obs(confidence="0.9")]),
        ("invalid_request", [_obs(observed_at="2026-11-01")]),
        ("invalid_request", [_obs(observed_at="2026-11-01T10:00:00")]),
        ("invalid_request", [_obs(observed_at="yesterday")]),
        ("target_not_declared", [_obs("no_such_field")]),
        ("target_not_declared", [_obs(), _obs("no_such_field")]),
    ],
)
def test_a_malformed_or_undeclared_observation_refuses_the_whole_request(
    code: str, observations: Any
) -> None:
    with pytest.raises(ConvergeRequestRefusedError) as refused:
        _resolve(_body(observations))
    assert refused.value.code == code


def test_observations_without_the_v2_contract_are_refused() -> None:
    body = _body([_obs()])
    del body["contract"]
    with pytest.raises(ConvergeRequestRefusedError) as refused:
        _resolve(body)
    assert refused.value.code == "v2_member_without_contract"


# ── Evidence semantics ─────────────────────────────────────────────────────────


def test_an_observation_is_observed_evidence_never_admitted_and_never_scored() -> None:
    plain = _obs()
    stated = _obs(
        POLYMERS, ["HDPE"], unit="list", observed_at="2026-11-01T10:00:00Z", confidence=0.6
    )
    evidence = observation_evidence([plain, stated], IDENTITY)
    assert list(evidence) == [TOLERANCE, POLYMERS]  # only the observed fields, nothing generalized
    assert evidence[TOLERANCE] == {
        "state": "observed",
        "admitted": False,
        "value": 0.02,
        "unit_status": "unknown",  # no unit was stated: not guessed
        "confidence": None,  # the consumer stated none, so there is none
        "sources": [
            {
                "source_type": "request_observation",
                "source_ref": "sale.order:1",
                "method": "transaction",
                "independent": None,
            }
        ],
        "conflict": None,
        "derivation": None,
        "unknown_reason": None,
        "identity": IDENTITY,
    }
    assert evidence[POLYMERS]["confidence"] == 0.6  # the consumer's statement, repeated as given
    assert (evidence[POLYMERS]["unit_status"], evidence[POLYMERS]["unit"]) == ("stated", "list")
    assert evidence[POLYMERS]["sources"][0]["observed_at"] == "2026-11-01T10:00:00Z"
    assert observation_evidence([], IDENTITY) == {}


def test_agreeing_observations_share_one_entry_and_list_every_source() -> None:
    evidence = observation_evidence(
        [_obs(value=2, confidence=0.5), _obs(value=2.0, source_ref="sale.order:2", confidence=0.9)],
        IDENTITY,
    )
    entry = evidence[TOLERANCE]
    assert entry["state"] == "observed" and entry["value"] == 2
    assert [s["source_ref"] for s in entry["sources"]] == ["sale.order:1", "sale.order:2"]
    assert entry["confidence"] is None  # two different statements: neither picked, nothing averaged
    same = observation_evidence(
        [_obs(confidence=0.5), _obs(source_ref="o:2", confidence=0.5)], IDENTITY
    )
    assert same[TOLERANCE]["confidence"] == 0.5


def test_disagreeing_observations_are_conflicting_and_carry_no_value() -> None:
    evidence = observation_evidence(
        [
            _obs(value=0.02),
            _obs(value=0.05, source_ref="sale.order:2"),
            _obs(value=0.02, unit="percent", source_ref="sale.order:3"),  # same number, other unit
            _obs(value=True, source_ref="sale.order:4"),  # a boolean is not the number 1
        ],
        IDENTITY,
    )
    entry = evidence[TOLERANCE]
    assert entry["state"] == "conflicting" and entry["admitted"] is False
    assert "value" not in entry and entry["confidence"] is None and entry["sources"] == []
    assert entry["conflict"]["resolution"] is None
    assert [c["value"] for c in entry["conflict"]["candidates"]] == [0.02, 0.05, 0.02, True]
    assert all(c["sources"] for c in entry["conflict"]["candidates"])
