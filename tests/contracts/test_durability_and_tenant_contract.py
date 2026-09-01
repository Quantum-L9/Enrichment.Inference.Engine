"""Runtime contract for the two guarantees a converge caller relies on.

A producer treats a completed `EnrichResponse` as the authoritative record of
its operation and does not ask again. Two guarantees make that safe, and both
were asserted in this repository's findings before either actually held on the
real Gate rail:

* **`completed` means durable.** A converge whose store write failed still
  answered `state="completed"` with no `failure_reason`, because the side-effect
  path was fire-and-forward. Gate turned that into a success, so the producer
  recorded an enrichment no store held. The once-per-key guard recorded the key
  anyway, so the retry that would have recovered it was suppressed as a
  duplicate.

* **Idempotency is per tenant.** `idempotency_key` carried a global UNIQUE and
  was looked up by key alone. The key is caller-chosen and unique only within
  its own tenant, so two tenants using the same string collided: the second
  tenant's converge resolved to the first tenant's stored row and was reported
  complete against a record it does not own.

The unit suites cover the mechanics. This file pins the *contract*: what a
caller is promised by a `state` value, and what a tenant boundary means — the
part that cannot be restored by reading the implementation back.

L9_META:
  tier: 2
  domain: convergence
  authority: L9 Master Kernel v3.0
  pr_class: app_code + tier2_test
"""

from __future__ import annotations

import importlib.util
import inspect
from unittest.mock import AsyncMock, patch

import pytest

# The constitution gate runs `tests/contracts/` in an environment that installs
# only constellation-node-sdk, so neither SQLAlchemy nor the Perplexity SDK is
# present there. Importing the app runtime at module scope would break
# collection of this whole file in that environment. Same convention as
# test_canonical_converge_runtime_contract.py: import the runtime lazily, guard
# on a real capability probe, and do NOT mark these `unit` — that marker means
# "no external dependencies", so the gate deselects them and the full
# `pytest tests/` job, which installs the project, runs them.
# Removal trigger: the gate installing the project's runtime deps.
_HAS_DB_RUNTIME = importlib.util.find_spec("sqlalchemy") is not None
_HAS_PROVIDER_RUNTIME = importlib.util.find_spec("perplexity") is not None

requires_db_runtime = pytest.mark.skipif(
    not _HAS_DB_RUNTIME,
    reason="the persistence models need SQLAlchemy; not installed in the contract gate env",
)
requires_app_runtime = pytest.mark.skipif(
    not (_HAS_DB_RUNTIME and _HAS_PROVIDER_RUNTIME),
    reason="app.engines.handlers needs the app runtime; not installed in the contract gate env",
)

ENTITY_REF = "res.partner:55"


def _payload(idempotency_key: str | None = None) -> dict:
    payload = {
        "entity": {"id": ENTITY_REF, "name": "Rotterdam Polymers BV"},
        "object_type": "res.partner",
        "objective": "Enrich this plastics recycler with firmographic fields.",
    }
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key
    return payload


# ── Contract 1: `state="completed"` is a durability claim ──────────────


@requires_app_runtime
@pytest.mark.asyncio
async def test_completed_is_never_returned_for_a_result_no_store_holds():
    from app.engines import handlers
    from app.models.schemas import EnrichResponse
    from app.services.side_effect_coordinator import PersistenceRequiredError

    async def _persist_fails(*_a, **_kw):
        raise PersistenceRequiredError("store unavailable")

    with (
        patch.object(handlers, "run_convergence_loop", new_callable=AsyncMock) as loop,
        patch.object(handlers, "_persist_and_sync", _persist_fails),
    ):
        loop.return_value = EnrichResponse(fields={"industry": "plastics"}, state="completed")
        result = await handlers.handle_converge("acme", _payload("op-1"))

    assert result["state"] != "completed", (
        "a producer reads state=='completed' as durable and stops asking; "
        "returning it for an unpersisted result loses the enrichment silently"
    )
    assert result["state"] == "failed"
    assert result["failure_reason"], "a non-completed state must say why"


@requires_app_runtime
@pytest.mark.asyncio
async def test_the_canonical_branch_demands_durability_rather_than_hoping_for_it():
    """The guarantee has to be requested at the call site, not left to chance."""
    from app.engines import handlers
    from app.models.schemas import EnrichResponse

    seen: dict = {}

    async def _record(*_a, **kwargs):
        seen.update(kwargs)

    with (
        patch.object(handlers, "run_convergence_loop", new_callable=AsyncMock) as loop,
        patch.object(handlers, "_persist_and_sync", _record),
    ):
        loop.return_value = EnrichResponse(fields={"x": 1}, state="completed")
        await handlers.handle_converge("acme", _payload())

    assert seen.get("require_persistence") is True


@requires_app_runtime
@pytest.mark.asyncio
async def test_a_non_durable_operation_stays_retryable():
    """No completion marker, or the recovery attempt is dropped as a duplicate."""
    from app.services.side_effect_coordinator import (
        PersistenceRequiredError,
        SideEffectCoordinator,
        semantic_side_effect_key,
    )

    coordinator = SideEffectCoordinator()

    class _Down:
        async def persist_enrich_response(self, **_kw):
            raise RuntimeError("store unavailable")

    with (
        patch("app.services.result_store.ResultStore", return_value=_Down()),
        patch("app.engines.packet_router.get_router") as router,
        patch("app.services.event_emitter.get_emitter") as emitter,
    ):
        router.return_value.notify_score_invalidate = AsyncMock()
        emitter.return_value.emit_enrichment_completed = AsyncMock()
        with pytest.raises(PersistenceRequiredError):
            await coordinator.commit_after_enrich(
                tenant="acme",
                entity_id=ENTITY_REF,
                object_type="res.partner",
                domain="plastics",
                response_dict={"fields": {}, "state": "completed"},
                settings=object(),
                idempotency_key="op-2",
                require_persistence=True,
            )

    key = semantic_side_effect_key(
        tenant="acme", entity_id=ENTITY_REF, action="enrich", idempotency_key="op-2"
    )
    assert key not in coordinator._completed


# ── Contract 2: a tenant boundary is a boundary ────────────────────────


@requires_db_runtime
def test_an_idempotency_key_is_unique_only_within_its_own_tenant():
    from sqlalchemy import UniqueConstraint

    from app.services.pg_models import EnrichmentResult

    uniques = [c for c in EnrichmentResult.__table__.constraints if isinstance(c, UniqueConstraint)]
    columns = [{col.name for col in c.columns} for c in uniques]

    assert {"tenant_id", "idempotency_key"} in columns, (
        f"expected UNIQUE(tenant_id, idempotency_key); got {columns}"
    )
    assert {"idempotency_key"} not in columns, (
        "a global UNIQUE(idempotency_key) makes one tenant's caller-chosen key "
        "collide with another's"
    )
    assert not EnrichmentResult.__table__.columns["idempotency_key"].unique


@requires_db_runtime
def test_a_replay_lookup_cannot_be_made_without_naming_a_tenant():
    from app.services import pg_store

    sig = inspect.signature(pg_store.get_enrichment_result_by_idempotency_key)
    assert "tenant_id" in sig.parameters
    assert sig.parameters["tenant_id"].default is inspect.Parameter.empty, (
        "an optional tenant is one a caller can omit, which restores the cross-tenant read"
    )


@requires_db_runtime
@pytest.mark.asyncio
async def test_one_tenants_replay_never_resolves_to_another_tenants_record():
    """The lookup is asked for the tenant being written, not for the key alone."""
    from app.services import pg_store

    with patch.object(
        pg_store, "get_enrichment_result_by_idempotency_key", new_callable=AsyncMock
    ) as lookup:
        lookup.return_value = object()
        await pg_store.save_enrichment_result(
            tenant_id="globex",
            entity_id=ENTITY_REF,
            object_type="res.partner",
            fields={},
            confidence=0.5,
            uncertainty_score=0.1,
            tokens_used=0,
            processing_time_ms=1,
            pass_count=1,
            idempotency_key="a-key-acme-also-uses",
        )

    assert lookup.await_args.kwargs.get("tenant_id") == "globex"
