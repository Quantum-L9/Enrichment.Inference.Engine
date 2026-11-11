"""Contract payloads accept any admitted domain spec, never a hard-coded one.

``FeatureEvidence.domain`` and ``EvidenceOdooMappingContract.domain`` were
``Literal["plasticos"]``: no other tenant's evidence could even be expressed.
They now accept exactly the domains whose spec the effective reader admits.

L9_META:
  tier: 2
  domain: enrichment
  authority: owner decision 2026-11-11 (the domain spec is the cartridge)
  pr_class: app_code + tier2_test
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.engines.domain_yaml_reader import DomainYamlReader
from app.models import domain_admission
from app.models.domain_admission import admitted_domain
from app.models.evidence_odoo_mapping import EvidenceOdooMappingContract
from app.models.feature_evidence import FeatureEvidence

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "contracts/feature_evidence/examples/feature-evidence.json"
MAPPING = ROOT / "contracts/evidence_odoo_mapping/evidence-odoo-mapping.yaml"

METALS = {
    "name": "metals",
    "version": "1.0.0",
    "ontology": {
        "nodes": [{"label": "Mill", "properties": [{"name": "mill_id", "type": "string"}]}]
    },
}


@pytest.fixture
def two_tenants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DomainYamlReader:
    root = tmp_path / "domains"
    for tenant, spec in {
        "metals": METALS,
        "plasticos": yaml.safe_load((ROOT / "domains/plasticos/spec.yaml").read_text()),
    }.items():
        (root / tenant).mkdir(parents=True)
        (root / tenant / "spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    reader = DomainYamlReader(root)
    monkeypatch.setattr(domain_admission, "effective_reader", lambda: reader)
    return reader


def _evidence(domain: str) -> dict[str, Any]:
    payload = json.loads(EXAMPLE.read_text())
    payload["domain"] = domain
    return payload


def test_feature_evidence_accepts_any_admitted_domain(two_tenants: DomainYamlReader) -> None:
    assert FeatureEvidence.model_validate(_evidence("metals")).domain == "metals"
    assert FeatureEvidence.model_validate(_evidence("plasticos")).domain == "plasticos"


@pytest.mark.parametrize("domain", ["copper", "../plasticos", "Plasticos", ""])
def test_feature_evidence_refuses_a_domain_without_an_admitted_spec(
    two_tenants: DomainYamlReader, domain: str
) -> None:
    with pytest.raises(ValidationError, match="domain"):
        FeatureEvidence.model_validate(_evidence(domain))


def test_mapping_contract_domain_is_validated_against_loaded_specs(
    two_tenants: DomainYamlReader,
) -> None:
    raw = yaml.safe_load(MAPPING.read_text())
    assert (
        EvidenceOdooMappingContract.model_validate({**raw, "domain": "metals"}).domain == "metals"
    )
    with pytest.raises(ValidationError, match="not an admitted domain spec"):
        EvidenceOdooMappingContract.model_validate({**raw, "domain": "copper"})


def test_admission_uses_the_reader_and_its_refusal(tmp_path: Path) -> None:
    reader = DomainYamlReader(tmp_path)
    with pytest.raises(ValueError, match="not an admitted domain spec"):
        admitted_domain("plasticos", reader)
    assert admitted_domain("plasticos", DomainYamlReader(ROOT / "domains")) == "plasticos"
