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
- [x] Alembic migrations — single tree at `migrations/`, guarded

**Remaining:** pgvector embedding storage is still not present. Coverage on
`pg_store.py` is 21.5% — the layer exists but is thinly exercised.

**Resolved (2026-08-23):** the orphaned second tree `alembic/` was deleted. Its
only revision, `0002_perplexity_api_key_default.py`, was inapplicable rather
than merely undiscovered: it mutated a `config_snapshots` table that exists
nowhere in this repository, and declared `down_revision = "0001"` against a
baseline whose revision is `"001"`. The concern it was written for — a safe
default for a missing Perplexity key — was solved at `app/core/config.py:25`
and is covered by `tests/integration/test_gap_fixes.py::TestPerplexityApiKeySafeDefault`.
Its sibling `0003` (same phantom table) was already dropped unmerged.
`alembic.ini` → `migrations/` is now the single tree, as
`docs/contracts/dependencies/postgresql.yaml` and
`docs/contracts/data/migrations/migration-policy.md` already declared, and
`tests/compliance/test_architecture.py` now fails on any revision outside it.

- [ ] **Schema drift: `uncertainty_score` type mismatch.** `migrations/versions/001_initial_schema.py`
      creates it as `sa.Float`; `app/services/pg_models.py` declares
      `Numeric(8, 4)`. This is real model-vs-schema drift and needs a migration.
      `migrations/env.py` sets `target_metadata` from `Base.metadata`, so
      `alembic revision --autogenerate` can detect it.

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

- [~] Gap-5 Audit Persistence — `app/services/audit_persistence.py` exists (40 stmts),
      is **not imported anywhere**, 0% coverage. Wiring it is **not** the two-line
      startup call it appears to be; two blockers were found on 2026-08-23:

      1. **No pool to give it.** `configure_audit_pool(pool)` expects a raw
         `asyncpg.Pool`, but the app has none — `pg_store.init_engine()` builds a
         SQLAlchemy `create_async_engine`, and `asyncpg.create_pool` appears
         nowhere in `app/`. Wiring as-is means opening a second connection pool to
         the same database. The right fix is to port the module to the existing
         SQLAlchemy engine, matching `pg_store.py` and `result_store.py`.
      2. **It does DDL at startup.** `configure_audit_pool` executes
         `CREATE TABLE IF NOT EXISTS audit_log` plus an index on every boot.
         `audit_log` exists in no migration and no model, so this bypasses Alembic
         entirely — at odds with the single-tree migration policy. The table needs
         a real migration and a `pg_models.py` entry first.
- [ ] Gap-6 Community Export Hook — `graph/community_export.py`
- [x] Gap-9 v1 Bridge Guard — **moot.** `shared/inference_bridge_v1_guard.py`
      never existed, and the thing it would have guarded against is gone:
      `app/engines/inference_bridge.py` (v1) was deleted once
      `inference_bridge_adapter.py`'s own migration steps 1–3 were verified
      complete. A guard against importing a module that no longer exists is
      not worth writing.

### Multi-Provider LLM Clients
- [~] `app/services/openai_client.py` — adapter built; NOT reachable from a request
- [~] `app/services/anthropic_client.py` — adapter built; NOT reachable from a request

**Partial (2026-08-23).** `OpenAISource` and `AnthropicSource` implement the
existing `BaseSource` protocol and are registered in `SOURCE_REGISTRY` as
`"openai"` and `"anthropic"`. Shared guard/quality/error behaviour lives in
`sources/llm_base.py` so the two adapters stay thin, and prompt construction
delegates to `prompt_builder.build_prompt` rather than duplicating it. The
`# TODO: Determine usage scope` comments in both dependency contracts are
answered: enrichment, not consensus.

Neither adapter picks a model — the clients' own defaults apply unless a
caller passes one explicitly. Wiring is not the place to make a
model-selection decision.

**These are marked `[~]`, not `[x]`, and the distinction is the point.**
Registration makes the adapters *resolvable*; it does not make them
*reachable*. Setting `OPENAI_API_KEY` today still cannot cause an enrichment
request to construct or call one. Verified:

- `WaterfallEngine(` is constructed in 10 places, **all under `tests/`**
- `auto_register_sources` has no caller outside `tests/`
- `config/enrichment_sources.yaml` lists clearbit, zoominfo, apollo, hunter,
  linkedin, perplexity_sonar — neither new provider
- all three `config/waterfall_config.yaml` tiers name `perplexity_sonar` only
- `app.main` invokes `enrichment_orchestrator` directly

**To close this item** someone must activate the waterfall path: construct a
`WaterfallEngine` at startup, call `auto_register_sources`, add both providers
to `enrichment_sources.yaml` and to the `waterfall_config.yaml` tiers with
priorities and quality thresholds, and decide how it composes with
`enrichment_orchestrator`. That changes enrichment behaviour for every
request, including the live Perplexity one, so it needs its own PR and review.
Do it AFTER the Perplexity envelope defect below, or activation puts a source
that can score 1.0 on zero merged fields into the live path.

**Also still open:** `consensus_engine.py` has no provider dispatch, so
multi-variation consensus across providers remains unbuilt.

### Perplexity response envelope is merged verbatim
- [ ] `app/services/perplexity_client.py` / `enrichment/sources/perplexity_adapter.py`

`prompt_builder` instructs the model to answer with an envelope —
`{"confidence": 0.82, "fields": {...}}` (see the example at
`prompt_builder.py:37`). `_parse_completion` at `perplexity_client.py:70` does
a bare `json.loads(content)` into `SonarResponse.data`, and the adapter
returns that object unchanged.

Two consequences on the **live** enrichment path:

1. The waterfall merges the literal keys `confidence` and `fields` instead of
   the requested field values.
2. Quality is scored over the envelope, so a response wrapping an empty
   `fields` object counts two populated keys and scores **1.0** — high enough
   to stop the waterfall having merged nothing.

`llm_base.unwrap_fields()` fixes this for the OpenAI/Anthropic adapters and is
the model for the fix here. Not applied to Perplexity in the same change
because Perplexity is the production path and altering its merge and scoring
is a behaviour change that deserves its own review. Regression tests to mirror:
`TestLLMEnvelopeAndClientReuse` in `tests/test_enrichment_sources.py`.

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

- [x] **Unreachable modules — triaged into three tiers, not one.** The original
      count was 686 statements across nine modules at 0% coverage, none loaded
      when `app.main` is imported. Per-module investigation showed they were three
      different problems, so they got three treatments:

      **Deleted (336 stmts, superseded — this PR):** `services/score/scorer.py`
      (+ its 0-byte `__init__.py`; it also shadowed the live `ScoreDimension`
      enum with an incompatible Pydantic model), `services/convergence_helpers.py`,
      `api/v1/intake.py` (had no `APIRouter` — never mountable), and
      `engines/inference_bridge.py` (the adapter's own documented step 4).
      `engines/inference_unlock_scorer.py` went with them, superseded by
      `engines/inference/rule_loader.py`, whose functions its claimed consumer
      `meta_prompt_planner.py:210` already imports.

      **To wire, not delete (202 stmts):** `services/openai_client.py`,
      `services/anthropic_client.py` and `services/audit_persistence.py` — see the
      Multi-Provider LLM Clients and Gap-5 items above. Declared architecture.

      **Staged (148 stmts):** `engines/inference/nary_inference_engine.py` — see
      the n-ary enablement plan below.
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
