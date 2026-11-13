"""`contracts/converge_request.json` is the live Odoo builder shape.

It is the exact shape IB-Odoo_19 (Staging @ 3ad2328)
`plasticos_gate/services/gate_builders.py::build_converge_request` produces,
and this test proves EIE accepts it on the canonical path. Since Odoo PR #163
that builder also emits `kb_context` (the domain), `schema` (its partner
writeback allowlist) and `idempotency_key` (`build_operation_id`); an earlier
revision of this fixture and test asserted their absence, which had become
false. docs/contracts/converge/v1.md specifies the shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.schemas import EnrichRequest

FIXTURE = Path(__file__).resolve().parents[2] / "contracts" / "converge_request.json"


@pytest.fixture(scope="module")
def payload() -> dict:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data.pop("_comment", None)
    return data


@pytest.mark.unit
def test_fixture_carries_the_odoo_envelope(payload: dict) -> None:
    assert set(payload) == {
        "entity",
        "object_type",
        "objective",
        "max_variations",
        "consensus_threshold",
        "kb_context",
        "schema",
        "idempotency_key",
        "odoo",
    }
    assert payload["consensus_threshold"] == 0.80
    assert payload["entity"]["_odoo_entity_id"] == payload["entity"]["id"]
    assert payload["odoo"]["model"] == "plasticos.enrichment.run"
    assert payload["kb_context"] == payload["object_type"] == "plasticos"
    # The eight partner fields Odoo may write back: the whole v1 field contract.
    assert payload["schema"] == dict.fromkeys(
        ["city", "email", "name", "phone", "street", "street2", "website", "zip"], "string"
    )
    odoo = payload["odoo"]
    assert payload["idempotency_key"] == (
        f"odoo:enrichment:{odoo['db_name']}:{odoo['model']}:{odoo['record_id']}"
    )


@pytest.mark.unit
def test_fixture_carries_identity_inside_the_canonical_entity(payload: dict) -> None:
    """The retired compat dialect put identity at the top level; the live payload never does."""
    assert isinstance(payload["entity"], dict)
    assert "entity_snapshot" not in payload
    assert "entity_id" not in payload


@pytest.mark.unit
def test_fixture_validates_as_the_canonical_enrich_request(payload: dict) -> None:
    """The live payload takes the canonical path: EnrichRequest, not the compat parser."""
    request = EnrichRequest.model_validate(payload)
    assert request.consensus_threshold == 0.80
    omitted = {key: value for key, value in payload.items() if key != "consensus_threshold"}
    assert EnrichRequest.model_validate(omitted).consensus_threshold == 0.65
    assert request.entity["name"] == "Acme Recycling"
    assert request.object_type == "plasticos"
    assert request.max_variations == 5
    assert request.schema_ == payload["schema"]
    assert request.kb_context == "plasticos"
    assert request.idempotency_key == payload["idempotency_key"]
    # The canonical branch keeps `entity` verbatim, Odoo alias included.
    assert request.entity["_odoo_entity_id"] == "res.partner:55"
