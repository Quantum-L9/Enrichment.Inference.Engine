"""Tier-2 contract: `EnrichRequest.schema` after F04 (request/response alignment).

F04 lets a v2 list/map target carry its typed declaration inside the internal
`schema` the converge loop runs on. This pins what that does and does not change
at the request/response contract:

* a v1 `schema` of type names is accepted and carried byte-for-byte unchanged;
* a schema value is a type name or a typed declaration (a mapping), never
  another JSON type;
* a v2 request resolves a scalar target to its type name and a list/map target
  to its declaration, while the v2 request grammar (`targets`) is unchanged;
* the response v2 `declaration` per field is unchanged (`spec_types.shape`).

EIE-NATIVE-CLOSURE-001 lane L2, F04.

L9_META:
  tier: 2
  domain: convergence
  authority: EIE-NATIVE-CLOSURE-001 lane L2, F04 (operator-approved)
  pr_class: tier2_test
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.engines.converge_request_v2 import field_declarations, resolve_converge_request_v2
from app.engines.domain_yaml_reader import DomainYamlReader
from app.engines.spec_types import shape
from app.models.schemas import EnrichRequest
from tests.unit.test_eie_native_e4_typed_evidence import MEMBERS, SPEC, TENANT, _body

pytestmark = [pytest.mark.unit, pytest.mark.enforcement]


@pytest.fixture
def reader(tmp_path: Path) -> DomainYamlReader:
    (tmp_path / TENANT).mkdir()
    (tmp_path / TENANT / "spec.yaml").write_text(yaml.safe_dump(SPEC), encoding="utf-8")
    return DomainYamlReader(tmp_path)


def _request(schema: Any) -> EnrichRequest:
    return EnrichRequest.model_validate(
        {"entity": {"name": "W"}, "object_type": "widget", "objective": "o", "schema": schema}
    )


@pytest.mark.parametrize(
    "schema",
    [
        {"recycling_grade": "string", "annual_tonnage": "number"},
        {"tags": "list", "active": "boolean"},
        json.dumps({"tags": "list"}),  # Salesforce LongTextArea form
    ],
)
def test_v1_type_name_schema_is_carried_unchanged(schema: Any) -> None:
    expected = json.loads(schema) if isinstance(schema, str) else schema
    assert _request(schema).schema_ == expected


@pytest.mark.parametrize("value", [5, 1.5, True, ["list"], None])
def test_a_schema_value_is_a_type_name_or_a_declaration(value: Any) -> None:
    with pytest.raises(ValidationError):
        _request({"tags": value})


def test_v2_resolution_keeps_scalars_as_names_and_containers_as_declarations(
    reader: DomainYamlReader,
) -> None:
    resolved = resolve_converge_request_v2(TENANT, _body(), reader)
    assert resolved is not None
    request = EnrichRequest.model_validate(resolved.body)
    assert request.schema_ == {"members": MEMBERS, "weight": "float"}
    # The response's per-field declaration is unchanged by F04.
    assert field_declarations(resolved)["members"] == shape({"name": "members", **MEMBERS})
    assert field_declarations(resolved)["weight"] == shape({"name": "weight", "type": "float"})
