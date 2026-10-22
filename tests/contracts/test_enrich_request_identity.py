"""SEAM-02 contract fixture — the EnrichRequest body CEG sends to EIE `enrich`.

This file is the shared fixture between the producer (CEG, SEAM-02 / E4a) and
the consumer (EIE `handle_enrich`, E4b). It pins:

1. the body shape the producer emits — top-level ``entity_id`` and ``domain``,
   plus ``entity.id`` and a filled ``idempotency_key``;
2. the identity resolution order the consumer applies — top-level ``entity_id``,
   then ``entity.id``, then ``entity.entity_id`` (``app.models.schemas.
   ENTITY_IDENTITY_ORDER``);
3. that a body naming none of them is rejected — EIE never substitutes the
   literal ``'unknown'`` for a missing identity.

Source: app/models/schemas.py, app/engines/handlers.py
Markers: unit
"""

from __future__ import annotations

import copy
import importlib.util
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.models.schemas import (
    ENTITY_IDENTITY_ORDER,
    EnrichRequest,
    EnrichResponse,
    resolve_entity_identity,
)

# `app.engines.handlers` reaches the Perplexity SDK transitively (handlers ->
# enrichment_orchestrator -> deterministic_provider -> perplexity_client ->
# `import perplexity`). The constitution gate runs `tests/contracts/` in an
# environment without the app's provider runtime, so a module-scope import
# would break collection of this whole file, schema contracts included (the
# same pattern as test_canonical_converge_runtime_contract.py). The body-shape
# and resolution-order contracts below run everywhere; the consumer-dispatch
# tests import the handlers lazily and declare the runtime they need. Removal
# trigger: the gate installing the project's runtime dependencies.
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
def test_handlers_module_has_no_unknown_identity_fallback_for_enrich() -> None:
    """`'unknown'` survives only in the converge compat resolver, never for enrich."""
    import inspect

    handlers = _handlers()
    for fn in (handlers._persist_and_sync, handlers.handle_enrich):
        source = inspect.getsource(fn)
        assert '"unknown"' not in source, fn.__name__
