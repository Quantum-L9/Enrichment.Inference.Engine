"""F04: a v2 list/map target's typed declaration reaches validation.

``converge_request_v2._targets`` used to reduce a ``list<map{...}>`` target to
the bare type name ``"list"``, and ``EnrichRequest.schema_`` (``dict[str, str]``)
could not carry more. ``validate_response`` already enforces a typed
declaration; it never received one. These witnesses run the real path,
resolve -> ``EnrichRequest`` -> ``validate_response``, on the domain-neutral
synthetic cartridge of ``test_eie_native_e4_typed_evidence``.

The collision authority's conditions are pinned here too: ``schema_`` stays a
mapping keyed by field name, so every existing reader (``_required_fields``,
``requested_fields``, the v2 field cut, ``_build_pass_request``,
``restrict_fields_to_schema``) behaves identically, and a v1 caller's
``{field: "list"}`` is unchanged.

L9_META:
  tier: 2
  domain: convergence
  authority: EIE-NATIVE-CLOSURE-001 lane L2, F04 (operator-approved lease addition)
  pr_class: app_code + tier2_test
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.engines.converge_contract import restrict_fields_to_schema
from app.engines.converge_request_v2 import resolve_converge_request_v2
from app.engines.convergence_controller import (
    ConvergenceState,
    _build_pass_request,
    _required_fields,
)
from app.engines.domain_yaml_reader import DomainYamlReader
from app.engines.meta_prompt_planner import PromptPlan
from app.models.schemas import EnrichRequest
from app.services.prompt_builder import build_prompt, build_schema_hash
from app.services.validation_engine import validate_response
from tests.unit.test_eie_native_e4_typed_evidence import MEMBERS, SPEC, TENANT, _body

pytestmark = pytest.mark.unit


@pytest.fixture
def reader(tmp_path: Path) -> DomainYamlReader:
    (tmp_path / TENANT).mkdir()
    (tmp_path / TENANT / "spec.yaml").write_text(yaml.safe_dump(SPEC), encoding="utf-8")
    return DomainYamlReader(tmp_path)


def _request(reader: Any) -> EnrichRequest:
    resolved = resolve_converge_request_v2(TENANT, _body(), reader)
    assert resolved is not None
    return EnrichRequest.model_validate(resolved.body)


def _through_the_request(reader: Any, members: list[Any]) -> dict[str, Any]:
    return validate_response(
        {"fields": {"members": members}, "confidence": 0.9}, _request(reader).schema_
    )


# ── The four end-to-end witnesses (failed at aabf855 and at c3bc05e) ─────────


def test_f04_undeclared_nested_member_is_dropped(reader: Any) -> None:
    out = _through_the_request(reader, [{"code": "A1", "line_id": "L1", "undeclared": 9}])
    assert out["members"] == [{"code": "A1", "line_id": "L1"}]


def test_f04_member_missing_required_code_is_refused(reader: Any) -> None:
    assert "members" not in _through_the_request(reader, [{"line_id": "L1"}])


def test_f04_plain_strings_are_refused_as_list_of_maps(reader: Any) -> None:
    assert "members" not in _through_the_request(reader, ["A1", "B2"])


def test_f04_int_line_id_is_encoded_as_declared_string(reader: Any) -> None:
    out = _through_the_request(reader, [{"code": "A1", "line_id": 411}])
    assert out["members"] == [{"code": "A1", "line_id": "411"}]


# ── What the request carries ────────────────────────────────────────────────


def test_f04_container_targets_carry_their_typed_declaration_scalars_stay_names(
    reader: Any,
) -> None:
    assert _request(reader).schema_ == {"members": MEMBERS, "weight": "float"}


def test_f04_v1_string_schema_is_unchanged() -> None:
    request = _v1({"tags": "list", "weight": "float"})
    assert request.schema_ == {"tags": "list", "weight": "float"}
    out = validate_response({"fields": {"tags": [" a ", "a"], "weight": "2"}}, request.schema_)
    assert (out["tags"], out["weight"]) == (["a"], 2.0)


@pytest.mark.parametrize("value", [5, ["list"], None])
def test_f04_a_schema_value_is_still_a_type_name_or_a_declaration(value: Any) -> None:
    with pytest.raises(ValidationError):
        _v1({"tags": value})


# ── Collision-authority condition 1: every reader behaves identically ───────

V1_FORM = {"members": "list", "weight": "float"}


def _v1(schema: dict[str, Any]) -> EnrichRequest:
    return EnrichRequest.model_validate(
        {"entity": {"name": "W"}, "object_type": "widget", "objective": "o", "schema": schema}
    )


def test_readers_key_on_field_names_identically(reader: Any) -> None:
    typed, named = _request(reader), _v1(V1_FORM)
    assert typed.schema_ is not None and named.schema_ is not None
    # convergence_controller.py:639 — _required_fields
    assert _required_fields(typed) == _required_fields(named) == ["members", "weight"]
    # convergence_controller.py:777-779 — feature_vector["requested_fields"]
    assert sorted(typed.schema_) == sorted(named.schema_)
    # convergence_controller.py:930-934 — the v2 field cut
    fields = {"members": [{"code": "A1"}], "weight": 1.0, "rogue": "x"}
    assert {k: v for k, v in fields.items() if k in typed.schema_} == {
        k: v for k, v in fields.items() if k in named.schema_
    }
    # handlers.py:468 — restrict_fields_to_schema
    a, b = {"fields": dict(fields)}, {"fields": dict(fields)}
    assert restrict_fields_to_schema(a, typed.schema_) == restrict_fields_to_schema(
        b, named.schema_
    )
    assert a == b == {"fields": {"members": [{"code": "A1"}], "weight": 1.0}}


def test_pass_request_keeps_the_typed_schema(reader: Any) -> None:
    # convergence_controller.py:1019-1020 — the caller's schema is the pass schema
    plan = PromptPlan(
        mode="targeted",
        priority_fields=["members"],
        kb_fragment_ids=[],
        variation_count=1,
        pass_number=1,
    )
    original = _request(reader)
    built = _build_pass_request(original, plan, ConvergenceState(), pass_num=1)
    assert built.schema_ == original.schema_
    assert "PRIORITY THIS PASS" in built.objective and "members" in built.objective


def test_prompt_and_schema_hash_accept_a_typed_declaration(reader: Any) -> None:
    schema = _request(reader).schema_
    payload = build_prompt(
        entity={"name": "W"}, object_type="widget", objective="o", target_schema=schema
    )
    assert "members" in str(payload) and "weight (float)" in str(payload)
    assert build_schema_hash(schema) == build_schema_hash(dict(schema or {}))


# ── Review follow-up (#394): a top-level map target is validated, not passed ──

PROFILE = {
    "type": "map",
    "properties": [
        {"name": "code", "type": "string", "required": True},
        {"name": "n", "type": "int"},
    ],
}


def test_a_top_level_map_missing_a_required_property_is_refused() -> None:
    out = validate_response({"fields": {"profile": {"n": 3}}}, {"profile": PROFILE})
    assert "profile" not in out


@pytest.mark.parametrize("value", ["oops", ["code"], 7])
def test_a_top_level_map_target_refuses_a_non_mapping(value: Any) -> None:
    assert "profile" not in validate_response({"fields": {"profile": value}}, {"profile": PROFILE})


def test_a_top_level_map_keeps_only_declared_typed_properties() -> None:
    out = validate_response(
        {"fields": {"profile": {"code": "A1", "n": "4", "extra": 1}}}, {"profile": PROFILE}
    )
    assert out["profile"] == {"code": "A1", "n": 4}
