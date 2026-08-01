# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- `odoo_modules/plasticos_research_enrichment/` — Odoo module v19.0.2.0.0 with async
  Perplexity enrichment pipeline, entropy engine, synthesis engine, and inference bridge
- Repo foundation files: LICENSE, CHANGELOG, SECURITY, GUARDRAILS, ARCHITECTURE, TESTING

### Changed
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
