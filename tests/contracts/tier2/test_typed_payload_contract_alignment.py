"""
Tier 2 — Typed payload contract alignment (owner decision 2026-11-11)
======================================================================
Pins two API-surface behaviours of the typed spec contract:

1. Evidence payload ``domain`` (``app/models/feature_evidence.py``,
   ``app/models/evidence_odoo_mapping.py``): any admitted domain spec id, never a
   hard-coded tenant. The published JSON schemas and the models agree — both
   take the tenant grammar, and the model additionally refuses a domain whose
   spec the effective reader does not admit.
2. Enrichment response normalisation (``app/services/validation_engine.py``):
   a list keeps its members' shape — map items stay maps, never
   ``"{'code': 'PET'}"`` strings — and a typed declaration in the shared spec
   syntax coerces each member to its declared item type.

Primary sources:
- contracts/feature_evidence/feature-evidence.schema.yaml
- contracts/evidence_odoo_mapping/evidence-odoo-mapping.schema.yaml
- app/models/domain_admission.py
- app/engines/spec_types.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.engines.domain_yaml_reader import TENANT_ID_PATTERN, DomainYamlReader
from app.models import domain_admission
from app.models.evidence_odoo_mapping import EvidenceOdooMappingContract
from app.models.feature_evidence import FeatureEvidence
from app.services.validation_engine import validate_response

pytestmark = [pytest.mark.unit, pytest.mark.enforcement]

ROOT = Path(__file__).resolve().parents[3]
FEATURE_SCHEMA = ROOT / "contracts/feature_evidence/feature-evidence.schema.yaml"
MAPPING_SCHEMA = ROOT / "contracts/evidence_odoo_mapping/evidence-odoo-mapping.schema.yaml"
FEATURE_EXAMPLE = ROOT / "contracts/feature_evidence/examples/feature-evidence.json"
MAPPING = ROOT / "contracts/evidence_odoo_mapping/evidence-odoo-mapping.yaml"


@pytest.fixture
def second_tenant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Admit a second tenant ('metals') next to plasticos in the effective reader."""
    root = tmp_path / "domains"
    specs = {
        "plasticos": yaml.safe_load((ROOT / "domains/plasticos/spec.yaml").read_text()),
        "metals": {
            "name": "metals",
            "version": "1.0.0",
            "ontology": {
                "nodes": [{"label": "Mill", "properties": [{"name": "mill_id", "type": "string"}]}]
            },
        },
    }
    for tenant, spec in specs.items():
        (root / tenant).mkdir(parents=True)
        (root / tenant / "spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    reader = DomainYamlReader(root)
    monkeypatch.setattr(domain_admission, "effective_reader", lambda: reader)


@pytest.mark.parametrize("schema_path", [FEATURE_SCHEMA, MAPPING_SCHEMA])
def test_published_schema_domain_is_the_tenant_grammar_not_a_tenant(schema_path: Path) -> None:
    domain = yaml.safe_load(schema_path.read_text())["properties"]["domain"]
    assert "const" not in domain and "enum" not in domain
    assert domain["pattern"] == TENANT_ID_PATTERN.pattern


def test_schema_and_model_accept_the_same_second_tenant(second_tenant: None) -> None:
    payload: dict[str, Any] = json.loads(FEATURE_EXAMPLE.read_text())
    payload["domain"] = "metals"
    Draft202012Validator(yaml.safe_load(FEATURE_SCHEMA.read_text())).validate(payload)
    assert FeatureEvidence.model_validate(payload).domain == "metals"

    mapping = {**yaml.safe_load(MAPPING.read_text()), "domain": "metals"}
    Draft202012Validator(yaml.safe_load(MAPPING_SCHEMA.read_text())).validate(mapping)
    assert EvidenceOdooMappingContract.model_validate(mapping).domain == "metals"


def test_model_refuses_a_well_formed_domain_with_no_admitted_spec(second_tenant: None) -> None:
    payload: dict[str, Any] = json.loads(FEATURE_EXAMPLE.read_text())
    payload["domain"] = "copper"
    # The schema checks shape only; admission against the loaded specs is the model's.
    Draft202012Validator(yaml.safe_load(FEATURE_SCHEMA.read_text())).validate(payload)
    with pytest.raises(ValidationError, match="not an admitted domain spec"):
        FeatureEvidence.model_validate(payload)


def test_response_normalisation_keeps_map_items_for_an_untyped_list() -> None:
    result = validate_response(
        {"fields": {"processed_polymers": [{"code": "PET"}, {"code": "HDPE"}]}, "confidence": 0.9},
        {"processed_polymers": "list"},
    )
    assert result["processed_polymers"] == [{"code": "PET"}, {"code": "HDPE"}]


def test_response_normalisation_applies_a_declared_list_item_type() -> None:
    declared = {
        "processed_polymers": {
            "type": "list",
            "items": {"type": "map", "properties": [{"name": "code", "type": "string"}]},
        },
        "certifications": {"type": "list", "items": {"type": "string"}},
    }
    result = validate_response(
        {
            "fields": {
                "processed_polymers": [{"code": "PET", "note": "dropped"}],
                "certifications": ["ISO 9001", " ISO 14001 "],
            }
        },
        declared,
    )
    assert result["processed_polymers"] == [{"code": "PET"}]
    assert result["certifications"] == ["ISO 9001", "ISO 14001"]
    # A member that is not the declared map is skipped as the field, never stringified.
    skipped = validate_response({"fields": {"processed_polymers": ["PET"]}}, declared)
    assert "processed_polymers" not in skipped
