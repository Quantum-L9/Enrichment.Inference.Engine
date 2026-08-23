# TODO — Enrichment.Inference.Engine

**Last Updated:** 2026-08-23
**Source:** Gap analysis of Core Gap Analysis-1.md, Core Gap Analysis-2.md (2026-04-07),
reconciled against the tree on 2026-08-23. Every status below was verified by
file existence, router mounting, or a coverage run — not carried forward on trust.

---

## Priority Legend

- 🔴 **CRITICAL** — Blocks production deployment
- 🟠 **HIGH** — Blocks full product functionality
- 🟡 **MEDIUM** — Enhances product value
- 🟢 **LOW** — Polish and optimization

Status markers: `[x]` shipped and exercised · `[~]` code exists but is not wired
into any live path · `[ ]` not started.

---

## 🔴 CRITICAL — Blocks Production

### PostgreSQL Persistence Layer
- [x] `app/services/pg_store.py` — connection pool + CRUD (402 lines)
- [x] `app/services/pg_models.py` — persistence models (227 lines, 100% covered)
- [x] `app/services/result_store.py` — durable enrichment records (221 lines)
- [~] Alembic migrations — **two competing trees; revision `0002` is orphaned**

**Remaining:** pgvector embedding storage is still not present. Coverage on
`pg_store.py` is 21.5% — the layer exists but is thinly exercised.

**Alembic defect:** `alembic.ini` sets `script_location = migrations`, so the
live chain is `migrations/env.py` + `migrations/versions/001_initial_schema.py`.
A second tree exists at `alembic/versions/0002_perplexity_api_key_default.py`
with no `alembic/env.py` beside it, so `alembic upgrade head` never discovers
it and the `0002` schema change is **not applied**. Either rebase `0002` into
`migrations/versions/` as a child of `001`, or delete it and document the
single wired tree. Until then this is not shipped.

### Event Emitter
- [x] `app/services/event_emitter.py` — publishes enrichment lifecycle events (232 lines)

### Async Task Queue
- [ ] `app/services/task_queue.py` — Celery/ARQ/Redis Streams for async processing

**Why:** Batch enrichment still runs synchronously. This is the only item in
this section that has not been started.

---

## 🟠 HIGH — Blocks Full Product

### Single chassis HTTP ingress (fix multi-path gates)
- [ ] **Problem:** One FastAPI process exposes many first-class routes (`/api/v1/enrich`, `/api/v1/enrich/batch`, discover, scan, proposals, `/v1/converge/*`, fields, etc.) *and* chassis routes (`POST /v1/execute`, `POST /v1/outcomes`). That violates the L9 "single ingress" model (constellation traffic should normalize on `POST /v1/execute` + health, not parallel REST surfaces).
- [ ] **Target:** Collapse external HTTP to chassis contract — e.g. `POST /v1/execute` (and documented health/readiness only); map CRM and internal flows through `TransportPacket` actions or a single adapter layer (deprecate direct `/api/v1/*` enrichment paths behind a migration window).
- [~] **Follow-through:** `app/score/score_api.py` is mounted
      (`app.include_router(score_router)` in `app/main.py`), but mounting is
      not wiring. All five providers in that module — `get_score_engine`,
      `get_decay_engine`, `get_explainer`, `get_profile_store`,
      `get_score_store` — unconditionally `raise DependencyNotConfiguredError`,
      and nothing sets `dependency_overrides` or configures them at startup, so
      every `/score/*` request fails at dependency resolution. Configuring
      those providers, and regenerating `docs/contracts/api/openapi.yaml` and
      `node.constitution.yaml` for the collapsed ingress, both remain open.

### Downstream Services (Constellation Nodes)

| Service | Purpose | Files Needed | Status |
|---------|---------|--------------|--------|
| **SIGNAL** | Engagement/intent signal detection | `app/signal/` (5 files) | not started |
| **ROUTE** | Lead routing from scores + territory | `app/route/` (5 files) | not started |
| **FORECAST** | Pipeline forecasting from enriched data | `app/forecast/` (5 files) | not started |
| **HANDOFF** | Automated handoff orchestration | `app/handoff/` (4 files) | not started |

### GRAPH Engine Gaps
- [x] Outcome recording endpoint (`POST /v1/outcomes`) — live in `app/api/v1/chassis_endpoint.py`; `app/services/outcome_delegator.py` is 100% covered
- [ ] CO-OCCURRED_WITH edge generation — collaborative filtering
- [ ] Entity resolution pre-match hook — deduplication before matching
- [ ] Graph→enrichment feedback channel — tell ENRICH which entities need work

### ENRICH ↔ GRAPH Integration
- [ ] Schema bridge utility — `DomainSpec.ontology.nodes.properties` → `EnrichRequest.schema`
- [ ] `enrichment_hints` in domain YAMLs — per-node-type config
- [ ] Pre-match enrichment wiring — ambiguous intake → enriched queries
- [ ] Outcome→enrichment delegation — rejection triggers re-enrichment

### gap-fixes/ Integration

Several components previously tracked as BLOCKED in `gap-fixes/` now exist under
`app/` and are covered by tests — `contract_enforcement.py` (100%),
`graph_return_channel.py` (92.6%), `inference_rule_registry.py` (61.9%),
`schema_proposer.py` (89.9%). Re-audit this table against `app/` before treating
any row as outstanding; the `gap-fixes/` staging directory is no longer the
source of truth for them.

- [~] Gap-5 Audit Persistence — `app/services/audit_persistence.py` exists (40 stmts) but is **not imported anywhere**; 0% coverage
- [ ] Gap-6 Community Export Hook — `graph/community_export.py`
- [ ] Gap-9 v1 Bridge Guard — `shared/inference_bridge_v1_guard.py`

### Multi-Provider LLM Clients
- [~] `app/services/openai_client.py` — file exists (152 lines) but is imported only by `anthropic_client.py`; 0% coverage
- [~] `app/services/anthropic_client.py` — file exists (154 lines), imported by nothing; 0% coverage

**Why still open:** both files exist, but neither is reachable from `app.main`,
so multi-provider consensus is not actually available at runtime. Formal
dependency contracts are declared for both in
`docs/contracts/dependencies/{openai,anthropic}.yaml` and asserted by
`tests/contracts/test_dependency_contracts.py`, so these are declared
architecture awaiting wiring — not dead code to delete.

### Transport / chassis router
- [x] `app/engines/packet_router.py` — exists (206 lines, 73.1% covered)

---

## 🟡 MEDIUM — Enhances Product

### Infrastructure
- [ ] Terraform IaC — `terraform/` directory for repeatable deployments
- [x] OpenTelemetry distributed tracing — `app/core/telemetry.py` (TracerProvider + OTLP exporter, 100% covered)
- [ ] Multi-tenant database isolation — PostgreSQL RLS enforcement

### API Endpoints
- [x] `POST /api/v1/discover` — `app/api/v1/discover.py`, router mounted
- [x] `GET /api/v1/fields/{entity_id}` — `app/api/v1/fields.py`, router mounted
- [x] `GET /v1/converge/{run_id}` — `app/api/v1/converge.py:294`
- [ ] `POST /api/v1/infer` — run inference rules independently
- [ ] `GET /api/v1/profile/{domain}` — get/set enrichment profiles per domain

### Testing
- [~] Integration tests with real Neo4j — `Neo4jContainer` fixture exists in `tests/integration/conftest.py` but skips when `testcontainers` is absent, which is the case in CI
- [ ] Contract tests (ENRICH ↔ GRAPH bidirectional validation)
- [ ] Load/stress tests — performance baselines

### Field Provenance
- [ ] `app/models/provenance.py` — track which pass/LLM/KB rule produced each field

---

## 🟢 LOW — Polish

### Convergence Loop
- [ ] Human-in-the-loop approval gate for schema changes (Discover tier) — note `POST /v1/converge/{run_id}/approve` exists as a route
- [ ] Domain KB hot-reload — add domains without restart

### Domain KB Expansion
- [ ] KB versioning and migration strategy
- [ ] KB validation schema (JSON Schema or Pydantic)
- [ ] Customer KB upload via API

### Documentation
- [ ] README.md with setup instructions
- [ ] CONTRIBUTING.md
- [ ] API documentation (OpenAPI/Swagger)

---

## 🔧 Engineering Debt (audit 2026-08-23)

Findings from the post-#179 ratchet audit. The ratchet ledgers themselves
(`.l9/baselines/test-quarantine.yml`, `.l9/baselines/packet-envelope.yml`) are
both empty — this is debt the ratchet does **not** cover.

- [ ] **Unreachable modules (686 statements, 0% coverage).** Nine modules are not
      loaded when `app.main` is imported: `api/v1/intake.py`,
      `engines/inference/nary_inference_engine.py`, `engines/inference_bridge.py`,
      `engines/inference_unlock_scorer.py`, `services/anthropic_client.py`,
      `services/audit_persistence.py`, `services/convergence_helpers.py`,
      `services/openai_client.py`, `services/score/scorer.py`. Removing them would
      raise coverage 69.37% → 75.07% with no tests written. **Seven of the nine are
      declared** in `docs/contracts/`, tests, or both, so this needs an owner
      decision (and likely an ADR) per module, not a bulk delete. Only
      `nary_inference_engine.py` and `convergence_helpers.py` have zero references
      anywhere.
- [ ] **Coverage is a floor, not a ratchet.** See the companion PR raising the
      threshold. Note `ci.yml` reads `vars.COVERAGE_THRESHOLD` first, so the repo
      variable must match the in-tree default to take effect.
- [ ] **Four xfail contract TODOs are invisible to the ratchet.**
      `tests/contracts/test_contract_todos_gaps.py` uses imperative
      `pytest.xfail()` (strict=False by design), so TODO-01, TODO-04, TODO-05 and
      TODO-10 report as passes. Nothing prevents more being added. TODO-10 is
      nearly closed — `/v1/converge/{run_id}` is missing only `state` and
      `pass_count`.
- [ ] **66 defensive `pytest.skip()` guards.** None fire today, but if a contract
      file were deleted ~20 contract tests would skip rather than fail.

---

## ✅ COMPLETED (Recent)

### GMP-ENRICH-001 — Consensus-Mode Enrichment (2026-03-30)
- [x] `app/services/enrichment/consensus.py` — Multi-response synthesis
- [x] `app/services/enrichment/uncertainty.py` — Confidence thresholds and flagging
- [x] `app/services/enrichment/kb_resolver.py` — KB context injection
- [x] `build_variation_prompts()` in `prompt_builder.py`
- [x] `enrich_with_consensus()` in `waterfall_engine.py`
- [x] `handle_enrich_consensus` handler in `handlers.py`
- [x] Unit tests for consensus, uncertainty, kb_resolver
- [x] Integration tests for consensus enrichment
- [x] `plastics_kb.yaml` test fixture
- [x] Enrichment package README documentation

### Test debt burndown (#139 / PR #179, 2026-08-23)
- [x] All 112 quarantined nodeids adapted to live APIs; `.l9/baselines/test-quarantine.yml` emptied
- [x] Cross-file `sys.modules` pollution in `tests/integration/test_consensus_enrichment.py` fixed
- [x] Suite green at 1451 passed / 4 xfailed, 69.37% coverage

### Previously Completed
- [x] HEALTH service — 5 files in `app/health/`
- [x] SCORE service — 6 files in `app/score/`
- [x] CI/CD pipelines — 22 workflow files
- [x] CRM field scanner — `crm_field_scanner.py`
- [x] Enrichment profiles — `enrichment_profile.py`
- [x] Cost tracking — `cost_tracker.py`
- [x] Pass-level telemetry — `pass_telemetry.py`
- [x] Confidence tracker — `confidence_tracker.py`
- [x] L9 chassis contract — `chassis_contract.py`, `handlers.py`

---

## Files to Delete (Obsolete)

Six of the seven originally listed have already been removed. Remaining:

- [ ] `plastics_enrichment_client.py` — standalone reference client at repo root, superseded by `WaterfallEngine`

---

## Revenue Impact per Gap Closure

| Gap Closed | Revenue Tier Unlocked | $/mo |
|-----------|----------------------|------|
| CRM field scanner + single discovery pass | **Seed** (free → conversion engine) | $0 (50%+ conversion to Enrich) |
| Enrichment profiles + nightly batch | **Enrich** | $500 |
| Convergence loop + schema proposals + approval gate | **Discover** | $2,000 |
| Graph service + outcome feedback + inference loop | **Autonomous** | $5,000–10,000 |
| HEALTH + SCORE + ROUTE | **RevOpsOS mid-market** | $10,000–25,000 |
| Full constellation with FORECAST + HANDOFF | **RevOpsOS enterprise** | $25,000–50,000 |
