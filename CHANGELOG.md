# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Changed
- **Seam-audit repairs (IB-Odoo_19 -> Gate_SDK -> Constellation.Gate -> EIE).**
  - SDK pin moves to the 1.1.0 release commit
    `2b2f53a28a59bbfb2fa45f5eac32b722d802209a`; every consumer of the
    coordinated set pins the same commit.
  - `GATE_URL` default is `http://localhost:9000`, the port every shipped Gate
    deployment asset uses; `8080` matched nothing.
  - Periodic Gate re-registration (`GATE_REREGISTRATION_INTERVAL_SECONDS`,
    default 60; 0 = register once). A worker Gate marked unhealthy on a
    connection failure recovers without a process restart, and a Gate that
    restarted with an empty registry regains the `converge` route.
  - Kubernetes (kustomize + helm) manifests carry `GATE_URL`,
    `GATE_REGISTRATION_ENABLED=true`, `GATE_INTERNAL_URL` and read
    `GATE_ADMIN_TOKEN` from the `enrichment-credentials` secret; without
    registration Gate had no route to this worker. `.env.example` and the
    env contract document the registration variables.
  - `Dockerfile.prod` builds from `pyproject.toml` with pip and installs
    `git` for the git+https SDK dependency; it previously copied a
    `poetry.lock` that does not exist and could not build.
  - `contracts/converge_request.json` is the exact payload the live Odoo
    builder emits, enforced by a test.
  - The node runtime reads its signing posture from `L9_REQUIRE_SIGNATURE`,
    `L9_SIGNING_KEY`, `L9_SIGNING_KEY_ID`, `L9_SIGNING_ALGORITHM` and
    `L9_VERIFYING_KEYS_JSON`. It never signed a response before, so a Gate
    that verifies worker responses rejected every EIE answer.

### Fixed
- **`confidence` no longer leaks into enriched fields.** Every normalized
  variation carries a `confidence` metadata key, so it won consensus on every
  pass and reached Odoo as an enriched field named `confidence`.
- **`variation_count` is a variation count**, the sum of each pass's planned
  variations, not a token total.
- **Packet router retries send a fresh packet.** Gate's replay guard rejects a
  reused `packet_id`, so every retry of the same packet was a guaranteed 400.
- **`create_all()` works on a fresh database.** `ConvergenceRun.state` declared
  both `index=True` and an explicit `Index("ix_convergence_runs_state")`,
  emitting two indexes with one name.

### Changed (earlier)
- **Gate registration is owned by the Gate_SDK.** The SDK pin moves to
  `bfe6642062a85a720ad8c25e96446d4df1c299ac`, the exact revision consumed by
  green Constellation.Gate PR #14, and `app/services/gate_registration.py` is
  deleted. EIE no longer implements a `POST /v1/admin/register` client, an
  admin-header construction, a registration retry loop, or a Gate status-code
  taxonomy: it supplies node values to `NodeRegistration` and hands them to
  `register_node()`. `metadata.owner=eie` — the sole reason the bespoke client
  existed — is now a first-class SDK field. Explicit lifecycle registration and
  the `gate_registered` readiness signal are unchanged.
- **The node cap EIE advertises to Gate is its own 25 s ceiling**, not the SDK's
  30 s default. Gate bounds a worker with `min(remaining budget, node cap)`, so
  the previous default told Gate to wait on time EIE had already abandoned.

### Fixed
- **`completed` now means durable.** A canonical converge whose PostgreSQL write
  failed still answered `state="completed"` with no `failure_reason`, because
  `_persist_and_sync` was fire-and-forward and the side-effect coordinator
  swallowed the persist exception — Gate turned that into a success and the
  enrichment was lost silently. The once-per-key guard recorded the key anyway,
  suppressing the retry that would have recovered it. Persistence is now a
  precondition of the canonical answer: the failure propagates, no downstream
  effect fires, no completion marker is left, and the handler returns a
  non-completed `EnrichResponse`. Non-canonical callers keep the previous
  fire-and-forward behaviour.
- **Idempotency is scoped to the tenant.** `enrichment_results.idempotency_key`
  carried a global `UNIQUE` and was looked up by key alone. An idempotency key is
  caller-chosen and unique only within its own tenant, so two tenants using the
  same string collided: the second tenant's converge resolved to the first
  tenant's stored enrichment and was reported complete against a row it does not
  own. Migration `002` replaces `UNIQUE(idempotency_key)` with
  `UNIQUE(tenant_id, idempotency_key)`, and the lookup now requires a tenant.
- **`alembic upgrade head` now commits.** `run_async_migrations` configured the
  context in one `run_sync` call and ran the migrations in a second lambda that
  discarded its connection, with no `context.begin_transaction()` — alembic
  logged `Running upgrade  -> 001` and exited 0 against a database left with zero
  tables. The shape now follows alembic's own async template.
- **EIE honours the upstream operation budget.** `handle_converge` called
  `Deadline.start(25)` unconditionally, so a dispatch packet carrying 2 s of
  remaining budget still opened a fresh 25 s operation. The effective budget is
  now `min(EIE ceiling, packet budget)`, read from the `header.timeout_ms` Gate
  bounds and the SDK passes to a three-parameter handler.
- **`scripts/validate_sdk_pin.py`** enforced the retired `a770e853` pin and never
  checked `requirements-ci.txt`, where CI resolves the SDK from.
- **The canonical converge contract is EnrichRequest in, EnrichResponse out.**
  `handle_converge` serves the payload the live Odoo producer emits
  (`entity` + `object_type` + `objective` + `max_variations`, identity on
  `entity["id"]`) by validating it as an `EnrichRequest` and returning
  `EnrichResponse.model_dump()` — no translation in either direction.
  Previously that payload was claimed by an adapter written for an older
  `entity_snapshot` dialect, which dropped `entity["id"]`, replaced the caller's
  `objective` and `object_type` with its own, reinterpreted `max_variations` as
  passes, and truncated the response `fields` to the eight partner keys. The
  adapter now matches the compatibility shapes only (`entity_snapshot`, or a
  top-level `entity_id`), so there is exactly one canonical contract.
- **Canonical Odoo converge is bounded end-to-end.** Odoo waits 30 s through Gate;
  EIE now finishes the complete request inside 25 s, holding 2 s back for response
  and error propagation.
  - The `timeout` threaded into `app/services/perplexity_client._sync_call` was
    accepted and never referenced, so `chat.completions.create()` ran under the
    Perplexity SDK default `Timeout(connect=5.0, read=900, ...)`. It is now applied
    to the real request via `with_options(timeout=..., max_retries=0)`.
  - The SDK's default `max_retries=2` compounded EIE's own 3-attempt loop into up to
    9 HTTP requests per variation. The pooled client is constructed with
    `max_retries=0` and every request re-asserts it: EIE is the sole retry owner.
  - New `app/services/request_deadline.py` — a monotonic deadline with a reserved
    tail, shared by the whole request. Each provider attempt is sized from what is
    left of it (capped at 20 s), and neither the retry loop nor the convergence loop
    starts work the remaining budget cannot cover.
  - The 25 s `asyncio.wait_for` wrapped only `run_convergence_loop`; domain-context
    loading, persistence and response assembly ran outside it. It now covers the
    complete operation — on the canonical branch, which is the one production takes.
  - Deadline exhaustion answers in `EnrichResponse` semantics (`state="failed"` with
    a `failure_reason`), which is what the reserved 2 s tail is for. Odoo's mapper
    derives its status from `state`, so a non-completed state routes the run to its
    own degraded handling; no Odoo-specific error envelope is invented.

### Added
- `odoo_modules/plasticos_research_enrichment/` — Odoo module v19.0.2.0.0 with async
  Perplexity enrichment pipeline, entropy engine, synthesis engine, and inference bridge
- Repo foundation files: LICENSE, CHANGELOG, SECURITY, GUARDRAILS, ARCHITECTURE, TESTING

### Changed
- **Graph sync is excluded from the canonical converge path.**
  `SideEffectCoordinator.commit_after_enrich` takes `graph_sync: bool = True`; both
  converge branches pass `False`. `notify_graph_sync` awaits up to three Gate attempts at
  a 30 s client timeout with backoff — 93 s worst case — which previously turned a
  *successful* enrichment into an `execution_timeout` for Odoo whenever Graph was
  degraded. Required EIE persistence stays synchronous; nothing is queued, deferred,
  or handed to a background task, and every other caller keeps prior behavior.
- **Feature-flag activation (full-throttle, test-proven).** Flipped 10 off-by-default
  flags to `true`; the flip was validated in an isolated worktree against the unit
  suite (baseline PASS → activated PASS, 0 regressions):
  - Enrichment dependency contracts `docs/contracts/dependencies/{apollo,clearbit,hunter,zoominfo}.yaml`
    `enabled: false → true` (descriptive attestation; runtime call still gated by the
    provider's `*_API_KEY` + `config/provider_config.yaml` — no live API call is turned
    on by this flip). Inline comments reconciled to state the real gate.
  - `config/enrichment_sources.yaml` `linkedin.enabled: false → true` (descriptive
    catalog; file is not imported by any module).
  - `infra/k8s/helm/enrichment-api/values-dev.yaml` `ingress`, `autoscaling`, `pdb`
    `enabled: false → true` (dev overlay only).
  - `monitoring/loki/loki-config.yaml` `auth_enabled: false → true` (Loki multi-tenant
    mode — **operators must send `X-Scope-OrgID` from Promtail/Grafana or log ingestion
    breaks**) and `analytics.reporting_enabled: false → true` (anonymous usage telemetry
    to Grafana Labs).

### Removed
- `.coderabbit.yaml` `auto_review.auto_pause_after_reviewed_commits` — removed so
  CodeRabbit never auto-pauses PR review (the default is "no pause"; the explicit key
  was redundant and read as a dangerous auto-behavior toggle).

### Security
- Held `GF_USERS_ALLOW_SIGN_UP=false` in `docker-compose.yml` (Grafana self-signup
  stays **off**) — deliberately excluded from the activation above.

---

## [2.3.0] — 2026-08-05

### Added
- **Explicit Gate registration lifecycle (TASK-003).** `app/services/gate_registration.py`
  builds the Gate admin-registration payload in-process and POSTs it to
  `{gate_url}/v1/admin/register?overwrite=true` during startup. Registration is
  feature-flagged (`gate_registration_enabled`, default `false`) and non-fatal:
  any failure is caught and surfaced, never crashing startup. New Settings fields:
  `gate_registration_enabled`, `gate_internal_url`, `gate_admin_token`.
- `HealthCheckResponse.gate_registered` (`bool | None`) surfaces registration
  outcome; `/api/v1/health` reports `status="degraded"` when registration failed.
- Canonical converge contract fixtures `contracts/converge_request.json` and
  `contracts/converge_response.json` with conformance tests (TASK-005).

### Changed
- **Version alignment to 2.3.0.** Reconciled lagging `2.2.0` declarations
  (`pyproject.toml`, `app/__init__.py`, `HealthCheckResponse.version`,
  k8s kustomize `app.kubernetes.io/version`, Helm `Chart.yaml`
  `version`/`appVersion`, and the `docs/FILE_INDEX_FOR_AGENTS.md` /
  `docs/INVARIANTS.md` VERSION headers) with the runtime literals already at
  `2.3.0`. The `EnrichResponse.inference_version` (`v2.2.0`) is a distinct
  contract-asserted artifact and is intentionally unchanged.

---

## [2.2.0] — 2026-03-30

### Added
- Universal domain-aware entity enrichment API (Salesforce + Odoo single ingress)
- Convergence controller with confidence tracking and cost tracking
- N-ary inference engine with rule engine and grade engine
- GDS scheduler integration
- OpenTelemetry instrumentation (OTLP/gRPC export)
- Waterfall enrichment pipeline with auto-register
- Schema proposer and uncertainty engine

### Changed
- Migrated to pydantic-settings v2
- Upgraded to FastAPI 0.115+

---

## [2.0.0] — 2026-01-15

### Added
- Initial enrichment orchestrator
- Perplexity sonar-reasoning integration
- Redis caching layer
- Graphiti knowledge graph sync client
