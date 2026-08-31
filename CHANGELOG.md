# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Fixed
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
