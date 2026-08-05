"""
Domain Enrichment API v2.3.0 — Constellation-wired
===================================================
POST /api/v1/enrich         single entity (Salesforce + Odoo)
POST /api/v1/enrich/batch   batch up to 50
GET  /api/v1/health         health + KB + circuit breaker
POST /v1/execute            SDK TransportPacket execution surface
POST /v1/outcomes           match outcome feedback

Integration fix applied (PR#22 merge pass):
    GAP-3: converge.configure() called in startup with LoopStateStore,
           ProfileRegistry, domain_specs, kb_resolver, and idem_store.
"""

from __future__ import annotations

import os
import time
from typing import Annotated

import structlog
from constellation_node_sdk import LifecycleHook, NodeRuntimeConfig, create_node_app
from constellation_node_sdk.runtime.handlers import clear_handlers
from fastapi import Depends

from .api.v1.attestation import router as attestation_router
from .api.v1.chassis_endpoint import router as chassis_router
from .api.v1.converge import router as converge_router
from .api.v1.discover import router as discover_router
from .api.v1.fields import router as fields_router
from .core.auth import verify_api_key
from .core.config import Settings, get_settings
from .core.logging_config import setup_logging
from .core.telemetry import setup_telemetry
from .engines.enrichment_orchestrator import breaker, enrich_batch, enrich_entity
from .engines.orchestration_layer import register as register_orchestration
from .middleware.rate_limiter import RateLimitMiddleware
from .models.schemas import (
    BatchEnrichRequest,
    BatchEnrichResponse,
    EnrichRequest,
    EnrichResponse,
    HealthCheckResponse,
)
from .score.score_api import router as score_router
from .services.idempotency import IdempotencyStore
from .services.kb_resolver import KBResolver

logger = structlog.get_logger("main")

_kb: KBResolver | None = None
_idem: IdempotencyStore | None = None
# Gate registration outcome (TASK-003): None = not attempted/disabled,
# True = accepted, False = rejected or errored (non-fatal).
_gate_registered: bool | None = None


class EnrichmentLifecycle(LifecycleHook):
    """Bridge the existing app startup/shutdown into the SDK runtime."""

    async def startup(self) -> None:
        """Load KB, connect Redis, init persistence, and register SDK handlers."""
        global _kb, _idem, _gate_registered

        settings = get_settings()
        setup_logging(settings.log_level)

        _kb = KBResolver(settings.kb_dir)

        try:
            _idem = IdempotencyStore(settings.redis_url)
            logger.info("redis_connected", url=settings.redis_url)
        except Exception as exc:
            logger.warning("redis_unavailable", error=str(exc))
            _idem = None

        clear_handlers()
        register_orchestration(kb=_kb, idem_store=_idem)
        from .services.chassis_handlers import register_all_handlers

        register_all_handlers()

        # Persistence layer
        from .services import pg_store
        from .services.event_emitter import get_emitter

        pg_store.init_engine(settings.database_url)
        get_emitter(settings)
        logger.info("pg_store_initialized")

        # GAP-3: Converge endpoint dependency injection
        from .api.v1 import converge as converge_module
        from .services.enrichment_profile import ProfileRegistry

        try:
            from .engines.convergence.loop_state import RedisLoopStateStore

            loop_state_store = (
                RedisLoopStateStore(redis_client=_idem.client) if _idem else _fallback_loop_store()
            )
        except Exception:
            loop_state_store = _fallback_loop_store()

        profile_registry = ProfileRegistry()
        converge_module.configure(
            state_store=loop_state_store,
            profile_registry=profile_registry,
            domain_specs={},
            kb_resolver=_kb,
            idem_store=_idem,
        )
        logger.info(
            "converge_module_configured",
            profiles=profile_registry.list_profiles(),
            state_backend="redis" if _idem else "memory",
        )

        # TASK-003: explicit, non-fatal Gate registration. auto_register_with_gate
        # is False, so we register here exactly once per process lifecycle.
        if _gate_registered is None:
            from .services.gate_registration import register_with_gate

            _gate_registered = await register_with_gate(settings)
            logger.info("gate_registration_attempted", result=_gate_registered)

        logger.info("api_started", version="2.3.0")

    async def shutdown(self) -> None:
        global _kb, _idem, _gate_registered

        if _idem:
            await _idem.close()
        from .services import pg_store as _pg

        await _pg.close_engine()
        _kb = None
        _idem = None
        # The Gate SDK exposes no deregister primitive; a stale Gate entry is
        # refreshed via overwrite=true on the next startup registration.
        _gate_registered = None


def _build_runtime_config() -> NodeRuntimeConfig:
    settings = get_settings()
    environment = os.getenv("L9_ENVIRONMENT", "local").strip().lower() or "local"
    return NodeRuntimeConfig(
        environment=environment,
        node_name="enrichment-engine",
        service_name="enrichment-engine",
        service_version="2.3.0",
        dev_mode=environment in {"local", "dev", "test"},
        gate_url=settings.gate_url,
        allowed_actions=(
            "community-export",
            "converge",
            "discover",
            "enrich",
            "enrich-and-sync",
            "enrichbatch",
            "graph-inference-result",
            "schema-proposal",
            "simulate",
            "writeback",
        ),
        max_attachments=0,
        # The SDK's field defaults are mutually inconsistent (attachment
        # default 10 MiB > packet default 256 KiB), so building the config
        # without explicit sizes raises a ValidationError at import time.
        # EIE disables attachments (max_attachments=0), so pin the
        # attachment ceiling to zero and keep the packet default explicit.
        max_packet_bytes=262_144,
        max_attachment_size_bytes=0,
    )


def _fallback_loop_store():
    """In-memory LoopStateStore used when Redis is unavailable."""
    from .engines.convergence.loop_state import LoopState, LoopStateStore

    class _InMemory(LoopStateStore):
        __slots__ = ("_data",)

        def __init__(self) -> None:
            super().__init__()
            self._data: dict[str, LoopState] = {}

        async def save(self, state: LoopState) -> None:
            self._data[state.run_id] = state

        async def load(self, run_id: str) -> LoopState | None:
            return self._data.get(run_id)

        async def list_active(self, domain: str | None = None) -> list[LoopState]:
            return [s for s in self._data.values() if domain is None or s.domain == domain]

    return _InMemory()


app = create_node_app(
    service_name="enrichment-engine",
    version="2.3.0",
    lifecycle_hook=EnrichmentLifecycle(),
    config=_build_runtime_config(),
    auto_register_with_gate=False,
)

app.add_middleware(RateLimitMiddleware, requests_per_minute=120)
setup_telemetry(app)

app.include_router(attestation_router)
app.include_router(chassis_router)
app.include_router(converge_router)
app.include_router(discover_router)
app.include_router(fields_router)
app.include_router(score_router)


@app.get("/api/v1/health", response_model=HealthCheckResponse)
async def health_check():
    kb = _kb or KBResolver("/dev/null")
    # A failed Gate registration degrades health; None (disabled/not attempted)
    # keeps the node "ok".
    status = "degraded" if _gate_registered is False else "ok"
    return HealthCheckResponse(
        status=status,
        version="2.3.0",
        kb_loaded=kb.index.is_loaded,
        kb_polymers=len(kb.index.polymers),
        kb_grades=kb.index.total_grades,
        kb_rules=kb.index.total_rules,
        circuit_breaker_state=breaker.state,
        gate_registered=_gate_registered,
    )


@app.post(
    "/api/v1/enrich",
    response_model=EnrichResponse,
    dependencies=[Depends(verify_api_key)],
)
async def enrich_single(
    request: EnrichRequest,
    settings: Annotated[Settings, Depends(get_settings)],
):
    return await enrich_entity(request, settings, _kb, _idem)


@app.post(
    "/api/v1/enrich/batch",
    response_model=BatchEnrichResponse,
    dependencies=[Depends(verify_api_key)],
)
async def enrich_batch_endpoint(
    request: BatchEnrichRequest,
    settings: Annotated[Settings, Depends(get_settings)],
):
    start = time.monotonic()
    results = await enrich_batch(request.entities, settings, _kb, _idem)
    elapsed = int((time.monotonic() - start) * 1000)
    succeeded = sum(1 for r in results if r.state == "completed")
    failed = sum(1 for r in results if r.state == "failed")
    tokens = sum(r.tokens_used for r in results)
    return BatchEnrichResponse(
        results=results,
        total=len(results),
        succeeded=succeeded,
        failed=failed,
        total_processing_time_ms=elapsed,
        total_tokens_used=tokens,
    )
