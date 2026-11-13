"""Converge request v2 resolution (docs/contracts/converge/request.v2.md, frozen 2026-11-13).

Every refusal code the resolver owns is exercised against the live PlasticOS
cartridge, plus a copy of it that declares a `subjects` vocabulary. Resolver
only: dispatch through the handler is proven where the handler is wired.

L9_META:
  tier: 2
  domain: convergence
  authority: EIE Two-Campaign Strategy Lock v0.1.0, Campaign A lane A3e
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
from app.engines.domain_yaml_reader import DomainYamlReader

pytestmark = pytest.mark.unit
DOMAINS = Path(__file__).resolve().parents[2] / "domains"
SPEC = yaml.safe_load((DOMAINS / "plasticos/spec.yaml").read_bytes())
SUBJECTS = {"types": [{"name": "facility", "node": "Facility"}], "roles": ["supplier", "buyer"]}


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "contract": REQUEST_CONTRACT_V2,
        "cartridge": {"id": "plasticos", "version": SPEC["version"]},
        "node": "Facility",
        "targets": [
            {"field": "contamination_tolerance", "type": {"type": "float"}, "min_confidence": 0.8},
            {"field": "processed_polymers"},
        ],
        "entity": {"id": "res.partner:55", "name": "Acme Recycling"},
        "object_type": "res.partner",
        "objective": "Enrich the facility profile",
        "odoo": {"record_id": 7},
    }
    body.update(overrides)
    return {key: value for key, value in body.items() if value is not ...}


@pytest.fixture
def reader() -> DomainYamlReader:
    return DomainYamlReader(DOMAINS)


@pytest.fixture
def subject_reader(tmp_path: Path) -> DomainYamlReader:
    """PlasticOS plus a declared subject vocabulary and a second node."""
    spec = yaml.safe_load(yaml.safe_dump(SPEC))
    spec["subjects"] = {
        **SUBJECTS,
        "types": [*SUBJECTS["types"], {"name": "material", "node": "Material"}],
    }
    spec["ontology"]["nodes"].append(
        {"label": "Material", "properties": [{"name": "material_id", "type": "string"}]}
    )
    (tmp_path / "plasticos").mkdir()
    (tmp_path / "plasticos/spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    return DomainYamlReader(tmp_path)


def test_a_v1_body_is_not_a_v2_request(reader: DomainYamlReader) -> None:
    v1 = {"entity": {"id": "x"}, "object_type": "plasticos", "objective": "o", "schema": {}}
    assert resolve_converge_request_v2("tenant-a", v1, reader) is None


def test_v2_resolves_to_the_pinned_cartridge_and_a_v1_shaped_body(reader: DomainYamlReader) -> None:
    resolved = resolve_converge_request_v2("plasticos", _body(), reader)

    assert resolved is not None
    assert resolved.config.domain_id == "plasticos"
    assert resolved.config.version == SPEC["version"]
    assert resolved.node == "Facility"
    assert resolved.subject is None
    # Types come from the cartridge's declarations, never from the caller.
    assert resolved.body == {
        "entity": {"id": "res.partner:55", "name": "Acme Recycling"},
        "object_type": "res.partner",
        "objective": "Enrich the facility profile",
        "odoo": {"record_id": 7},
        "domain_id": "plasticos",
        "node_label": "Facility",
        "schema": {"contamination_tolerance": "float", "processed_polymers": "list"},
        "field_thresholds": {"contamination_tolerance": 0.8},
    }


def test_declared_subject_resolves_as_opaque_strings(subject_reader: DomainYamlReader) -> None:
    body = _body(subject={"type": "facility", "role": "supplier"})
    resolved = resolve_converge_request_v2("plasticos", body, subject_reader)
    assert resolved is not None
    assert resolved.subject == {"type": "facility", "role": "supplier"}


LIST_OF_MAPS = {
    "type": "list",
    "items": {"type": "map", "properties": [{"name": "code", "type": "string", "required": True}]},
}

REFUSALS: list[tuple[str, dict[str, Any]]] = [
    ("unsupported_contract", {"contract": "eie.converge.request/v3"}),
    ("invalid_request", {"surprise": 1}),
    ("invalid_request", {"cartridge": {"id": "plasticos"}}),
    ("invalid_request", {"cartridge": {"id": "systemwide", "version": "1.0.0"}}),
    ("invalid_request", {"node": ...}),
    ("invalid_request", {"targets": []}),
    ("invalid_request", {"targets": [{"field": "tier", "type": "string"}]}),
    ("invalid_request", {"targets": [{"field": "tier", "min_confidence": 1.5}]}),
    ("conflicting_members", {"schema": {"tier": "string"}}),
    ("conflicting_members", {"field_thresholds": {"tier": 0.5}}),
    ("conflicting_members", {"domain": "other-cartridge"}),
    ("conflicting_members", {"node_label": "Material"}),
    ("tenant_cartridge_mismatch", {"cartridge": {"id": "other-tenant", "version": "1.0.0"}}),
    ("cartridge_pin_mismatch", {"cartridge": {"id": "plasticos", "version": "9.9.9"}}),
    (
        "cartridge_pin_mismatch",
        {"cartridge": {"id": "plasticos", "version": SPEC["version"], "sha256": "ab" * 32}},
    ),
    ("node_not_declared", {"node": "Warehouse"}),
    ("target_not_declared", {"targets": [{"field": "credit_score"}]}),
    ("target_type_mismatch", {"targets": [{"field": "tier", "type": {"type": "float"}}]}),
    (
        "target_type_mismatch",
        {
            "targets": [
                {
                    "field": "processed_polymers",
                    "type": {"type": "list", "items": {"type": "string"}},
                }
            ]
        },
    ),
    ("duplicate_target", {"targets": [{"field": "tier"}, {"field": "tier"}]}),
    # A subject type no PlasticOS revision declares is refused, whether or not the
    # loaded revision declares a subject vocabulary.
    ("subject_not_declared", {"subject": {"type": "vessel"}}),
]


@pytest.mark.parametrize(
    ("code", "change"), REFUSALS, ids=[f"{c}-{i}" for i, (c, _) in enumerate(REFUSALS)]
)
def test_refusals(code: str, change: dict[str, Any], reader: DomainYamlReader) -> None:
    with pytest.raises(ConvergeRequestRefusedError) as refused:
        resolve_converge_request_v2("plasticos", _body(**change), reader)
    assert refused.value.code == code
    assert str(refused.value).startswith(f"{code}: ")


def test_matching_container_type_is_accepted(reader: DomainYamlReader) -> None:
    """Shape for shape: a nested `required` is part of the shared typed shape."""
    body = _body(targets=[{"field": "processed_polymers", "type": LIST_OF_MAPS}])
    assert resolve_converge_request_v2("plasticos", body, reader) is not None


def test_tenant_mismatch_is_refused_before_any_cartridge_is_read() -> None:
    with pytest.raises(ConvergeRequestRefusedError) as refused:
        resolve_converge_request_v2("tenant-a", _body(), None)
    assert refused.value.code == "tenant_cartridge_mismatch"


def test_unknown_cartridge_and_missing_reader_are_not_found(tmp_path: Path) -> None:
    for reader in (DomainYamlReader(tmp_path), None):
        with pytest.raises(ConvergeRequestRefusedError) as refused:
            resolve_converge_request_v2("plasticos", _body(), reader)
        assert refused.value.code == "cartridge_not_found"


@pytest.mark.parametrize("member", ["cartridge", "node", "subject", "targets"])
def test_v2_member_without_contract_is_refused(member: str, reader: DomainYamlReader) -> None:
    v1 = {"entity": {"id": "x"}, "object_type": "plasticos", "objective": "o", member: "anything"}
    with pytest.raises(ConvergeRequestRefusedError) as refused:
        resolve_converge_request_v2("plasticos", v1, reader)
    assert refused.value.code == "v2_member_without_contract"


@pytest.mark.parametrize(
    ("code", "subject", "node"),
    [
        ("subject_not_declared", {"type": "carrier"}, "Facility"),
        ("subject_not_declared", {"type": "facility", "role": "broker"}, "Facility"),
        ("subject_target_mismatch", {"type": "material"}, "Facility"),
        ("invalid_request", {"role": "supplier"}, "Facility"),
    ],
)
def test_subject_refusals(
    code: str, subject: dict[str, str], node: str, subject_reader: DomainYamlReader
) -> None:
    with pytest.raises(ConvergeRequestRefusedError) as refused:
        resolve_converge_request_v2("plasticos", _body(subject=subject, node=node), subject_reader)
    assert refused.value.code == code
