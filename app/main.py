"""
Domain Enrichment API v2.3.0 — Constellation-wired
===================================================
POST /api/v1/enrich         single entity (Salesforce + Odoo)
POST /api/v1/enrich/batch   batch up to 50
GET  /api/v1/health         liveness: health + KB + circuit breaker (always 200)
GET  /api/v1/ready          readiness: 503 while Gate holds no route to this node
POST /v1/execute            SDK TransportPacket execution surface
(Outcome feedback flows CEG -> Gate -> EIE as a TransportPacket; the
 former POST /v1/outcomes peer ingress was retired 2026-09-02.)

Integration fix applied (PR#22 merge pass):
    GAP-3: converge.configure() called in startup with LoopStateStore,
           ProfileRegistry, domain_specs, kb_resolver, and idem_store.
"""

from __future__ import annotations

import os
import time
from typing import Annotated

import structlog
from constellation_node_sdk import (
    LifecycleHook,
    NodeRegistration,
    NodeRuntimeConfig,
    create_node_app,
    get_runtime_config,
)
from constellation_node_sdk.runtime.handlers import clear_handlers
from fastapi import Depends, Response

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
    ReadinessResponse,
)
from .score.score_api import router as score_router
from .services.idempotency import IdempotencyStore
from .services.kb_resolver import KBResolver
from .services.request_deadline import CANONICAL_CONVERGE_BUDGET_SECONDS

logger = structlog.get_logger("main")

_kb: KBResolver | None = None
_idem: IdempotencyStore | None = None


class EnrichmentLifecycle(LifecycleHook):
    """Bridge the existing app startup/shutdown into the SDK runtime."""

    async def startup(self) -> None:
        """Load KB, connect Redis, init persistence, and register SDK handlers."""
        global _kb, _idem

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

        # Gate participation (registration, re-registration after a Gate
        # restart, readiness) is owned by the SDK: create_node_app(registration=...)
        # below. L9-PARTICIPATION-01.

        logger.info("api_started", version="2.3.0")

    async def shutdown(self) -> None:
        global _kb, _idem

        if _idem:
            await _idem.close()
        from .services import pg_store as _pg

        await _pg.close_engine()
        _kb = None
        _idem = None


# EIE's registration semantics. EIE owns these values; the Gate_SDK owns
# validating, rendering, retrying and transporting them. Advertised actions are
# deliberately a subset of NodeRuntimeConfig.allowed_actions: a runtime that
# *permits* an action is not a claim that the node serves it well enough to be
# routed one. The invariant is advertised ⊆ implemented ⊆ runtime_allowed.
NODE_NAME = "enrichment-engine"
NODE_VERSION = "2.3.0"
NODE_TYPE = "enrichment"
NODE_OWNER = "eie"
HEALTH_ENDPOINT = "/api/v1/health"
# EIE-212-F003: the endpoint Kubernetes readiness probes read. Kept apart from
# HEALTH_ENDPOINT (liveness, and what Gate polls) so a Gate outage takes this
# pod out of Service rotation without restarting it.
READINESS_ENDPOINT = "/api/v1/ready"
ADVERTISED_ACTIONS: tuple[str, ...] = (
    "converge",
    "graph-inference-result",
    "enrich",
    "enrich-and-sync",
)
# Cluster-internal service address, reached over the container network rather
# than the public internet, and Gate's registration schema accepts both schemes
# for exactly that reason. A deployment terminating TLS between nodes sets
# GATE_INTERNAL_URL explicitly; hard-coding https here would break every node
# that does not. Overridden by settings.gate_internal_url in build_node_registration.
_DEFAULT_INTERNAL_URL = f"http://{NODE_NAME}:8000"  # NOSONAR

# The node cap Gate applies when it bounds a worker attempt
# (min(remaining packet budget, node cap)). It is EIE's own complete-operation
# ceiling expressed in the control plane, so Gate never hands EIE a budget EIE
# would not honour, and never waits on one EIE has already abandoned. Advertising
# the SDK default of 30 s here would reintroduce the split this closure removes.
NODE_TIMEOUT_MS = int(CANONICAL_CONVERGE_BUDGET_SECONDS * 1000)


def build_node_registration(settings: Settings) -> NodeRegistration:
    """EIE's Gate control-plane identity, as an SDK NodeRegistration.

    Values only — no HTTP, no retry loop, no status taxonomy. `owner` is a
    first-class SDK field: Gate resolves the semantic owner of a canonical
    action from `metadata.owner` first, and the SDK renders it there.
    """
    internal_url = (settings.gate_internal_url or _DEFAULT_INTERNAL_URL).strip().rstrip("/")
    return NodeRegistration(
        node_name=NODE_NAME,
        internal_url=internal_url,
        supported_actions=ADVERTISED_ACTIONS,
        health_endpoint=HEALTH_ENDPOINT,
        version=NODE_VERSION,
        node_type=NODE_TYPE,
        owner=NODE_OWNER,
        timeout_ms=NODE_TIMEOUT_MS,
    )


RUNTIME_ALLOWED_ACTIONS: tuple[str, ...] = (
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
)


def _build_runtime_config() -> NodeRuntimeConfig:
    """EIE's SDK runtime configuration.

    The SDK environment contract (`get_runtime_config`: L9_REQUIRE_SIGNATURE,
    L9_SIGNING_KEY / _KEY_ID / _ALGORITHM, L9_VERIFYING_KEYS_JSON,
    L9_ENFORCE_GATE_ONLY_INGRESS, ...) is the base. Building the config from a
    hand-written field list dropped every signing field, so outside `local` the
    SDK preflight rejected startup: Gate-only ingress requires a verified
    signature and none was configured (seam audit 2026-09-02, EIE-DEPLOY-01).
    EIE overrides only what EIE owns -- its identity, its action surface, and
    its attachment policy -- through the validated constructor, never a copy
    that skips validators.
    """
    settings = get_settings()
    base = get_runtime_config()
    environment = base.environment
    return NodeRuntimeConfig(
        **{
            **base.model_dump(),
            "environment": environment,
            "node_name": NODE_NAME,
            "service_name": NODE_NAME,
            "service_version": NODE_VERSION,
            # Local/dev/test run unsigned by default; the SDK forbids dev_mode
            # in staging/prod, where L9_REQUIRE_SIGNATURE must be set.
            "dev_mode": base.dev_mode or environment in {"local", "dev", "test"},
            "gate_url": settings.gate_url or None,
            "allowed_actions": RUNTIME_ALLOWED_ACTIONS,
            "max_attachments": 0,
            "max_packet_bytes": base.max_packet_bytes,
            "max_attachment_size_bytes": 0,
        }
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


def _bridge_settings_to_participation_env(settings: Settings) -> None:
    """Give Gate_SDK the Gate controls Settings already loaded.

    Pydantic reads `.env` and `.env.local` into Settings and does not export
    them. `NodeParticipation.from_env` reads `GATE_ADMIN_TOKEN` and
    `GATE_REGISTRATION_ENABLED` from the process environment, defaulting the
    enable flag to true when the variable is absent. A process started with
    those controls only in the documented env files would register without
    the admin token, or would attempt registration that Settings had switched
    off. A variable already present in the process environment stays
    authoritative (Kubernetes and the shell).
    """
    if "GATE_REGISTRATION_ENABLED" not in os.environ:
        os.environ["GATE_REGISTRATION_ENABLED"] = (
            "true" if settings.gate_registration_enabled else "false"
        )
    if settings.gate_admin_token and "GATE_ADMIN_TOKEN" not in os.environ:
        os.environ["GATE_ADMIN_TOKEN"] = settings.gate_admin_token


_participation_settings = get_settings()
_bridge_settings_to_participation_env(_participation_settings)
app = create_node_app(
    service_name="enrichment-engine",
    version="2.3.0",
    lifecycle_hook=EnrichmentLifecycle(),
    config=_build_runtime_config(),
    registration=build_node_registration(_participation_settings),
)

app.add_middleware(RateLimitMiddleware, requests_per_minute=120)
setup_telemetry(app)

app.include_router(attestation_router)
app.include_router(chassis_router)
app.include_router(converge_router)
app.include_router(discover_router)
app.include_router(fields_router)
app.include_router(score_router)


GATE_REGISTRATION_DEGRADED: frozenset[str] = frozenset({"failed", "not_attempted"})


def _legacy_gate_registered(sdk_state: str) -> bool | None:
    """Public `gate_registered` contract on top of the SDK state.

    null is disabled or not yet attempted (including `registering`). true is
    Gate accepted this node. false is reserved for a rejected or errored
    attempt. Clients that still read this field must not treat startup or an
    intentional disable as a failure.
    """
    if sdk_state == "active":
        return True
    if sdk_state == "degraded":
        return False
    return None


def _gate_registration_state() -> str:
    """Gate_SDK's participation state, named in EIE's registration vocabulary.

    EIE-002 still holds: "never attempted" is not "switched off". The SDK now
    owns registration, re-registration after a Gate restart and the state
    (L9-PARTICIPATION-01); EIE only renders it for /api/v1/health and
    /api/v1/ready, whose contract (names, 200 vs 503) is unchanged.
    """
    state = app.state.participation.status.state.value
    return {"active": "registered", "degraded": "failed", "disabled": "disabled"}.get(
        state, "not_attempted"
    )


@app.get("/api/v1/health", response_model=HealthCheckResponse)
async def health_check(settings: Annotated[Settings, Depends(get_settings)]):
    kb = _kb or KBResolver("/dev/null")
    registration = _gate_registration_state()
    status = "degraded" if registration in GATE_REGISTRATION_DEGRADED else "ok"
    return HealthCheckResponse(
        status=status,
        gate_registration=registration,
        version="2.3.0",
        kb_loaded=kb.index.is_loaded,
        kb_polymers=len(kb.index.polymers),
        kb_grades=kb.index.total_grades,
        kb_rules=kb.index.total_rules,
        circuit_breaker_state=breaker.state,
        gate_registered=_legacy_gate_registered(app.state.participation.status.state.value),
    )


@app.get(
    READINESS_ENDPOINT,
    response_model=ReadinessResponse,
    responses={
        503: {"model": ReadinessResponse, "description": "Gate holds no route to this node"}
    },
)
async def readiness_check(
    settings: Annotated[Settings, Depends(get_settings)],
    response: Response,
):
    """Readiness is routability, expressed as a status code a probe can read.

    EIE-212-F003: health_check above reports `degraded` in JSON and still
    returns 200, so a readinessProbe pointed at it marked an unroutable pod
    Ready. This endpoint answers 503 for exactly the two states health calls
    degraded — registration enabled but `failed` or `not_attempted` — and 200
    for `registered` and `disabled`. The process stays alive either way; only
    the Service stops sending it traffic Gate could not route anyway.
    """
    registration = _gate_registration_state()
    ready = registration not in GATE_REGISTRATION_DEGRADED
    response.status_code = 200 if ready else 503
    return ReadinessResponse(
        ready=ready,
        status="ready" if ready else "not_ready",
        gate_registration=registration,
        version=NODE_VERSION,
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
