"""Converge response v2 members on EnrichResponse stay aligned across model, API contract and fixture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.engines.convergence.field_evidence import RESPONSE_CONTRACT_V2, check_response_v2
from app.models.schemas import EnrichResponse

pytestmark = [pytest.mark.unit, pytest.mark.enforcement, pytest.mark.provenance]

_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES = _ROOT / "tests" / "contracts" / "fixtures"
_V2_KEYS = {"contract_version", "field_evidence", "proposals"}
_STATES = {"observed", "inferred", "external_claim", "conflicting", "unknown"}


def _fixture(name: str) -> dict[str, Any]:
    loaded = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _openapi_response() -> dict[str, Any]:
    spec = yaml.safe_load((_ROOT / "docs/contracts/api/openapi.yaml").read_text(encoding="utf-8"))
    schema = spec["components"]["schemas"]["EnrichResponse"]
    assert isinstance(schema, dict)
    return schema


def test_v2_fixture_is_a_valid_response_and_obeys_the_producer_rules() -> None:
    payload = _fixture("converge_response_v2.json")
    assert payload["contract_version"] == RESPONSE_CONTRACT_V2
    model = EnrichResponse.model_validate(payload)
    assert model.model_dump() == payload  # nothing dropped, nothing added
    evidence = payload["field_evidence"]
    check_response_v2(payload, list(evidence))
    assert {entry["state"] for entry in evidence.values()} == _STATES
    for name, entry in evidence.items():
        assert ("value" in entry) == (entry["state"] not in {"unknown", "conflicting"}), name
        assert entry["identity"]["subject_ref"], name


def test_api_contract_declares_every_v2_member_and_keeps_them_optional() -> None:
    schema = _openapi_response()
    assert set(schema["properties"]) >= _V2_KEYS
    assert not _V2_KEYS & set(schema.get("required", []))
    assert schema["properties"]["contract_version"]["enum"] == [RESPONSE_CONTRACT_V2]
    # additionalProperties is false: a v2 answer is valid only because the members are declared.
    assert schema["additionalProperties"] is False
    assert set(_fixture("converge_response_v2.json")) <= set(schema["properties"])


def test_v1_fixture_and_v1_serialization_carry_no_v2_member() -> None:
    v1 = _fixture("enrich_response_example.json")
    assert not _V2_KEYS & set(v1)
    assert not _V2_KEYS & set(EnrichResponse.model_validate(v1).model_dump())
    assert set(EnrichResponse.model_fields) >= _V2_KEYS
