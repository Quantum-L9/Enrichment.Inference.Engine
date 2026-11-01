"""Regression coverage for JSON schema before-validators."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.loop_schemas import ConvergeRequest
from app.models.schemas import EnrichRequest


def test_converge_target_schema_json_object_string_is_parsed() -> None:
    request = ConvergeRequest(
        entity={"Name": "Acme"},
        object_type="Account",
        objective="enrich",
        target_schema='{"industry": "string"}',
    )

    assert request.target_schema == {"industry": "string"}


def test_converge_target_schema_malformed_string_returns_none() -> None:
    request = ConvergeRequest(
        entity={"Name": "Acme"},
        object_type="Account",
        objective="enrich",
        target_schema="not-json",
    )

    assert request.target_schema is None


def test_converge_target_schema_non_object_json_retains_validation_error() -> None:
    with pytest.raises(ValidationError):
        ConvergeRequest(
            entity={"Name": "Acme"},
            object_type="Account",
            objective="enrich",
            target_schema='["industry"]',
        )


def test_converge_target_schema_dict_passes_through() -> None:
    schema = {"industry": "string"}

    request = ConvergeRequest(
        entity={"Name": "Acme"},
        object_type="Account",
        objective="enrich",
        target_schema=schema,
    )

    assert request.target_schema == schema


def test_enrich_schema_json_object_string_is_parsed_via_validation_alias() -> None:
    request = EnrichRequest(
        entity={"Name": "Acme"},
        object_type="Account",
        objective="enrich",
        schema='{"industry": "string"}',
    )

    assert request.schema_ == {"industry": "string"}


def test_enrich_schema_malformed_or_non_object_json_returns_none() -> None:
    malformed = EnrichRequest(
        entity={"Name": "Acme"},
        object_type="Account",
        objective="enrich",
        schema="not-json",
    )
    non_object = EnrichRequest(
        entity={"Name": "Acme"},
        object_type="Account",
        objective="enrich",
        schema='["industry"]',
    )

    assert malformed.schema_ is None
    assert non_object.schema_ is None


def test_enrich_schema_dict_passes_through_validation_alias() -> None:
    schema = {"industry": "string"}

    request = EnrichRequest(
        entity={"Name": "Acme"},
        object_type="Account",
        objective="enrich",
        schema=schema,
    )

    assert request.schema_ == schema
