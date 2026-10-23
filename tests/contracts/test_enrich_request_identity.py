"""SEAM-02 contract fixture — the EnrichRequest body CEG sends to EIE `enrich`.

This file is the shared fixture between the producer (CEG, SEAM-02 / E4a) and
the consumer (EIE `handle_enrich`, E4b). It pins:

1. the body shape the producer emits — top-level ``entity_id`` and ``domain``,
   plus ``entity.id`` and a filled ``idempotency_key``;
2. the identity resolution order the consumer applies — top-level ``entity_id``,
   then ``entity.id``, then ``entity.entity_id`` (``app.models.schemas.
   ENTITY_IDENTITY_ORDER``);
3. that a body naming none of them is rejected — EIE never substitutes the
   literal ``'unknown'`` for a missing identity;
4. that the converge consumer resolves identity exactly once — the SEAM-02
   order plus the live Odoo builder's ``entity._odoo_entity_id`` as the
   documented last fallback (``CONVERGE_IDENTITY_ORDER``) — and keys the
   convergence loop and persistence on that one id; the compatibility dialect
   refuses an identity-less body before any provider call;
5. that the published OpenAPI contract for EnrichRequest names every model
   field, so the body this fixture pins is also the body the contract admits.

Source: app/models/schemas.py, app/engines/handlers.py, docs/contracts/api/openapi.yaml
Markers: unit
"""

from __future__ import annotations

import copy
import importlib.util
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.models.schemas import (
    CONVERGE_IDENTITY_ORDER,
    ENTITY_IDENTITY_ORDER,
    EnrichRequest,
    EnrichResponse,
    resolve_converge_identity,
    resolve_entity_identity,
)
from tests.contracts.conftest_contracts import API_DIR, load_yaml

# The constitution gate runs `tests/contracts/` without the provider runtime
# that `app.engines.handlers` imports transitively; the schema contracts below
# run everywhere and the consumer-dispatch tests import the handlers lazily.
# Rationale and removal trigger: test_canonical_converge_runtime_contract.py.
_HAS_PROVIDER_RUNTIME = importlib.util.find_spec("perplexity") is not None
requires_app_runtime = pytest.mark.skipif(
    not _HAS_PROVIDER_RUNTIME,
    reason="app.engines.handlers needs the Perplexity SDK; not installed in the contract gate env",
)
pytestmark = pytest.mark.unit


def _handlers():
    """Import the SDK handler module lazily. See the note above."""
    from app.engines import handlers

    return handlers


TENANT = "plasticos"
ENTITY = "res.partner:55"

# The body the CEG producer sends (SEAM-02). Keep in lockstep with the
# producer-side fixture in Cognitive.Engine.Graphs.
ENRICH_REQUEST_SEAM_02: dict[str, Any] = {
    "entity_id": ENTITY,
    "domain": "plasticos",
    "entity": {
        "id": ENTITY,
        "name": "Acme Recycling",
        "website": "https://acme.example",
    },
    "object_type": "res.partner",
    "objective": "Fill the gaps CEG found for this partner",
    "schema": {"certifications": "list", "annual_capacity_tons": "integer"},
    "idempotency_key": "ceg:plasticos:res.partner:55:7c1d0e9a",
}


def _body(**overrides: Any) -> dict[str, Any]:
    body = copy.deepcopy(ENRICH_REQUEST_SEAM_02)
    body.update(overrides)
    return body


# ── 1. Body shape ────────────────────────────────────────────────────────────


def test_fixture_carries_top_level_identity_and_domain() -> None:
    assert ENRICH_REQUEST_SEAM_02["entity_id"] == ENRICH_REQUEST_SEAM_02["entity"]["id"]
    assert ENRICH_REQUEST_SEAM_02["domain"] == TENANT
    assert ENRICH_REQUEST_SEAM_02["idempotency_key"]


def test_fixture_validates_as_enrich_request_with_identity_fields() -> None:
    request = EnrichRequest.model_validate(ENRICH_REQUEST_SEAM_02)
    assert request.entity_id == ENTITY
    assert request.domain == TENANT
    assert request.entity["id"] == ENTITY
    assert request.idempotency_key == ENRICH_REQUEST_SEAM_02["idempotency_key"]
    assert request.schema_ == {"certifications": "list", "annual_capacity_tons": "integer"}


def test_identity_fields_are_optional_on_the_http_surface() -> None:
    """Salesforce / Odoo / Clay post neither field; the model must still accept them."""
    body = _body()
    del body["entity_id"]
    del body["domain"]
    request = EnrichRequest.model_validate(body)
    assert request.entity_id is None
    assert request.domain is None


# ── 2. Resolution order ──────────────────────────────────────────────────────


def test_resolution_order_is_pinned() -> None:
    assert ENTITY_IDENTITY_ORDER == ("entity_id", "entity.id", "entity.entity_id")


def test_top_level_entity_id_wins() -> None:
    body = _body(entity_id="top", entity={"id": "mid", "entity_id": "low"})
    assert resolve_entity_identity(body) == "top"


def test_entity_id_inside_entity_is_second() -> None:
    body = _body(entity={"id": "mid", "entity_id": "low"})
    del body["entity_id"]
    assert resolve_entity_identity(body) == "mid"


def test_entity_entity_id_is_the_last_fallback() -> None:
    body = _body(entity={"entity_id": "low", "name": "x"})
    del body["entity_id"]
    assert resolve_entity_identity(body) == "low"


@pytest.mark.parametrize(
    "body",
    [
        {"entity": {"name": "no identity"}, "object_type": "x", "objective": "y"},
        {"entity": {}, "object_type": "x", "objective": "y"},
        {"entity_id": "", "entity": {"id": "  ", "entity_id": ""}, "object_type": "x"},
        {"entity_id": None, "entity": {"id": None}, "object_type": "x", "objective": "y"},
        {"entity_id": 55, "entity": {"id": 55}, "object_type": "x", "objective": "y"},
    ],
)
def test_no_identity_resolves_to_none_never_unknown(body: dict[str, Any]) -> None:
    assert resolve_entity_identity(body) is None


# ── 3. Consumer: handle_enrich rejects a body with no identity ───────────────


def _completed() -> EnrichResponse:
    return EnrichResponse(fields={"website": "acme.example"}, confidence=0.9, state="completed")


@requires_app_runtime
@pytest.mark.asyncio
async def test_handle_enrich_rejects_body_without_identity_before_enrichment() -> None:
    handlers = _handlers()
    enricher = AsyncMock(return_value=_completed())
    persist = AsyncMock()
    body = {"entity": {"name": "Acme"}, "object_type": "res.partner", "objective": "x"}
    with (
        patch.object(handlers, "enrich_entity", enricher),
        patch.object(handlers, "_persist_and_sync", persist),
        pytest.raises(ValueError, match="entity identity required"),
    ):
        await handlers.handle_enrich(TENANT, body)
    enricher.assert_not_awaited()
    persist.assert_not_awaited()


@requires_app_runtime
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (_body(), ENTITY),
        (
            {k: v for k, v in _body(entity={"id": "mid"}).items() if k != "entity_id"},
            "mid",
        ),
        (
            {k: v for k, v in _body(entity={"entity_id": "low"}).items() if k != "entity_id"},
            "low",
        ),
    ],
)
async def test_handle_enrich_persists_under_the_resolved_identity(
    body: dict[str, Any], expected: str
) -> None:
    handlers = _handlers()
    coordinator = AsyncMock()
    with (
        patch.object(handlers, "enrich_entity", AsyncMock(return_value=_completed())),
        patch(
            "app.services.side_effect_coordinator.get_side_effect_coordinator",
            return_value=coordinator,
        ),
    ):
        result = await handlers.handle_enrich(TENANT, body)

    assert result["state"] == "completed"
    coordinator.commit_after_enrich.assert_awaited_once()
    kwargs = coordinator.commit_after_enrich.await_args.kwargs
    assert kwargs["tenant"] == TENANT
    assert kwargs["entity_id"] == expected
    assert kwargs["entity_id"] != "unknown"
    assert kwargs["idempotency_key"] == body.get("idempotency_key")


@requires_app_runtime
@pytest.mark.asyncio
async def test_persist_and_sync_never_keys_on_unknown() -> None:
    """The shared side-effect path refuses an unidentified payload outright."""
    handlers = _handlers()
    coordinator = AsyncMock()
    with (
        patch(
            "app.services.side_effect_coordinator.get_side_effect_coordinator",
            return_value=coordinator,
        ),
        pytest.raises(ValueError, match="entity identity required"),
    ):
        await handlers._persist_and_sync(
            TENANT, {"entity": {"name": "x"}}, {"state": "completed"}, "res.partner"
        )
    coordinator.commit_after_enrich.assert_not_awaited()


@requires_app_runtime
def test_handlers_module_has_no_unknown_identity_fallback() -> None:
    """No consumer path substitutes `'unknown'` for a missing identity.

    An identity-less body is refused before the provider is called; nothing
    downstream (loop, GraphReturnChannel, persistence) ever sees a placeholder.
    """
    import inspect

    handlers = _handlers()
    for fn in (
        handlers._persist_and_sync,
        handlers.handle_enrich,
        handlers._handle_canonical_converge,
        handlers._run_canonical_converge,
    ):
        source = inspect.getsource(fn)
        assert '"unknown"' not in source, fn.__name__


# ── 4. Consumer: converge resolves identity once, before any provider call ──
#
# The canonical converge body is the EnrichRequest the live Odoo builder emits:
# identity rides on `entity` (`id`, with `_odoo_entity_id` as the builder's
# alias), never at the top level. The SEAM-02 producer may also send the
# top-level `entity_id`. One resolver serves all of them, in one order, and the
# id it returns is the id the loop, GraphReturnChannel and persistence key on.


def _converge_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "entity": {"id": ENTITY, "name": "Acme Recycling"},
        "object_type": "plasticos",
        "objective": "Converge the partner record",
        "schema": {"website": "string"},
    }
    body.update(overrides)
    return body


def test_converge_identity_order_is_seam_02_plus_the_odoo_alias() -> None:
    assert (*ENTITY_IDENTITY_ORDER, "entity._odoo_entity_id") == CONVERGE_IDENTITY_ORDER


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"entity_id": "top", "entity": {"id": "mid", "_odoo_entity_id": "alias"}}, "top"),
        ({"entity": {"id": "mid", "entity_id": "low", "_odoo_entity_id": "alias"}}, "mid"),
        ({"entity": {"entity_id": "low", "_odoo_entity_id": "alias"}}, "low"),
        ({"entity": {"name": "Acme", "_odoo_entity_id": " alias "}}, "alias"),
    ],
)
def test_resolve_converge_identity_applies_seam_02_then_the_odoo_alias(
    body: dict[str, Any], expected: str
) -> None:
    assert resolve_converge_identity(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        {"entity": {"name": "no identity"}},
        {"entity": {"_odoo_entity_id": "  "}},
        {"entity_id": "", "entity": {"id": None, "entity_id": "", "_odoo_entity_id": 55}},
        {"entity_id": None},
    ],
)
def test_resolve_converge_identity_is_none_when_nothing_names_one(body: dict[str, Any]) -> None:
    assert resolve_converge_identity(body) is None


@requires_app_runtime
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(_converge_body(), ENTITY, id="entity.id"),
        pytest.param(
            _converge_body(entity={"name": "Acme Recycling", "_odoo_entity_id": ENTITY}),
            ENTITY,
            id="odoo-alias-only",
        ),
        pytest.param(
            _converge_body(entity_id="TOP", entity={"id": ENTITY, "name": "Acme Recycling"}),
            "TOP",
            id="top-level-wins-over-entity.id",
        ),
        pytest.param(
            _converge_body(entity={"entity_id": "low", "name": "Acme Recycling"}),
            "low",
            id="entity.entity_id",
        ),
    ],
)
async def test_canonical_converge_keys_loop_and_persistence_on_one_identity(
    body: dict[str, Any], expected: str
) -> None:
    handlers = _handlers()
    loop = AsyncMock(return_value=_completed())
    coordinator = AsyncMock()
    with (
        patch.object(handlers, "run_convergence_loop", loop),
        patch(
            "app.services.side_effect_coordinator.get_side_effect_coordinator",
            return_value=coordinator,
        ),
    ):
        result = await handlers.handle_converge(TENANT, body)

    assert result["state"] == "completed"
    loop.assert_awaited_once()
    assert loop.await_args.kwargs["entity_id"] == expected
    assert loop.await_args.kwargs["tenant_id"] == TENANT
    coordinator.commit_after_enrich.assert_awaited_once()
    kwargs = coordinator.commit_after_enrich.await_args.kwargs
    assert kwargs["tenant"] == TENANT
    assert kwargs["entity_id"] == expected
    assert kwargs["require_persistence"] is True


@requires_app_runtime
@pytest.mark.asyncio
async def test_compat_converge_refuses_body_without_identity_before_provider() -> None:
    """The `entity_snapshot` dialect is held to the same bar: no identity, no provider call."""
    handlers = _handlers()
    loop = AsyncMock(return_value=_completed())
    persist = AsyncMock()
    body = {"entity_snapshot": {"name": "Acme Recycling"}, "domain": "plasticos"}
    with (
        patch.object(handlers, "run_convergence_loop", loop),
        patch.object(handlers, "_persist_and_sync", persist),
    ):
        result = await handlers.handle_converge(TENANT, body)

    assert result["state"] == "failed"
    assert "entity_id is required" in result["failure_reason"]
    loop.assert_not_awaited()
    persist.assert_not_awaited()


@requires_app_runtime
@pytest.mark.asyncio
async def test_compat_converge_keys_loop_and_persistence_on_one_identity() -> None:
    handlers = _handlers()
    loop = AsyncMock(return_value=_completed())
    coordinator = AsyncMock()
    body = {"entity_id": ENTITY, "domain": "plasticos"}
    with (
        patch.object(handlers, "run_convergence_loop", loop),
        patch(
            "app.services.side_effect_coordinator.get_side_effect_coordinator",
            return_value=coordinator,
        ),
    ):
        result = await handlers.handle_converge(TENANT, body)

    assert result["state"] == "completed"
    loop.assert_awaited_once()
    assert loop.await_args.kwargs["entity_id"] == ENTITY
    coordinator.commit_after_enrich.assert_awaited_once()
    assert coordinator.commit_after_enrich.await_args.kwargs["entity_id"] == ENTITY


# ── 5. Contract doc: OpenAPI EnrichRequest names every model field ───────────


def _wire_name(field_name: str) -> str:
    """`schema` is a reserved BaseModel attribute, so the model stores it as `schema_`."""
    return "schema" if field_name == "schema_" else field_name


def test_openapi_enrich_request_is_in_lockstep_with_the_model() -> None:
    """docs/contracts/api/openapi.yaml#EnrichRequest admits exactly the model's fields.

    The schema is closed (`additionalProperties: false`), so a model field the
    document does not name is a body the published contract rejects — including
    the SEAM-02 `entity_id` / `domain` envelope this fixture pins.
    """
    spec = load_yaml(API_DIR / "openapi.yaml")
    documented = spec["components"]["schemas"]["EnrichRequest"]
    assert documented.get("additionalProperties") is False

    documented_fields = set(documented["properties"])
    model_fields = {_wire_name(name) for name in EnrichRequest.model_fields}
    assert model_fields - documented_fields == set(), "model-only fields missing from openapi.yaml"
    assert documented_fields - model_fields == set(), "openapi.yaml names fields the model lacks"

    for name in ("entity_id", "domain"):
        assert documented["properties"][name]["type"] == "string"
        assert documented["properties"][name].get("nullable") is True
