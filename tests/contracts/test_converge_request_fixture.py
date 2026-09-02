"""`contracts/converge_request.json` is the live Odoo builder shape.

The fixture previously carried a Salesforce-style Account payload with
`kb_context` and a top-level `idempotency_key`, neither of which the live
IB-Odoo_19 builder emits. It is now the exact shape
`plasticos_gate/services/gate_builders.py::build_converge_request` produces,
and this test proves EIE accepts it on the canonical path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.schemas import EnrichRequest
from app.services.odoo_gate_converge import is_odoo_compat_converge_payload, odoo_entity_ref

FIXTURE = Path(__file__).resolve().parents[2] / "contracts" / "converge_request.json"


@pytest.fixture(scope="module")
def payload() -> dict:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data.pop("_comment", None)
    return data


@pytest.mark.unit
def test_fixture_carries_the_odoo_envelope(payload: dict) -> None:
    assert set(payload) == {"entity", "object_type", "objective", "max_variations", "odoo"}
    assert payload["entity"]["_odoo_entity_id"] == payload["entity"]["id"]
    assert payload["odoo"]["model"] == "plasticos.enrichment.run"
    assert "kb_context" not in payload
    assert "idempotency_key" not in payload


@pytest.mark.unit
def test_fixture_is_not_diverted_to_the_compatibility_path(payload: dict) -> None:
    assert is_odoo_compat_converge_payload(payload) is False


@pytest.mark.unit
def test_fixture_validates_as_the_canonical_enrich_request(payload: dict) -> None:
    """The live payload takes the canonical path: EnrichRequest, not the compat parser."""
    request = EnrichRequest.model_validate(payload)
    assert request.entity["name"] == "Acme Recycling"
    assert request.object_type == "plasticos"
    assert request.max_variations == 5
    # The canonical branch keeps `entity` verbatim; the compat resolver must
    # NOT claim this payload (that was the lossy routing the audit found).
    assert odoo_entity_ref(payload) is None
    assert request.entity["_odoo_entity_id"] == "res.partner:55"
