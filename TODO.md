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

- `WaterfallEngine(` is constructed in 12 places, **all under `tests/`**
- `auto_register_sources` has no caller outside `tests/`
- `config/enrichment_sources.yaml` lists clearbit, zoominfo, apollo, hunter,
  linkedin, perplexity_sonar — neither new provider
- all three `config/waterfall_config.yaml` tiers name `perplexity_sonar` only
- `app.main` invokes `enrichment_orchestrator` directly

**To close this item** the waterfall path must be activated. That is a project,
not a wiring task. A full audit found **twelve** blockers, several of which mean
the path does not merely sit unused — it does not currently work at all:

| # | Blocker | Evidence |
|---|---|---|
| 1 | `auto_register_sources()` **crashes on its own default path**. `config/provider_config.yaml` is a YAML *list*; the code does `cfg.get("providers", {}).items()` | `provider_config.yaml:1-18` vs `waterfall_engine.py:118-119` |
| 2 | Same file uses `provider:` and `api_url:`; the loader wants a dict key and `base_url:` | `provider_config.yaml:2,4` vs `waterfall_engine.py:137` |
| 3 | **No env var reaches a `SourceConfig`.** `CLEARBIT/ZOOMINFO/APOLLO/HUNTER/OPENAI/ANTHROPIC_API_KEY` are declared and read by nothing; adapters read `self.config.api_key`, populated only from YAML | `core/config.py:56-62` |
| 4 | `enrichment_sources.yaml` has **no reader anywhere**. The `sources_config_path` parameter actually wants `waterfall_config.yaml` | `waterfall_engine.py:71,98-99` |
| 5 | `gong_ai`, `linkedin`, `clay` are not in `SOURCE_REGISTRY`, so the `opportunity` tier degrades to perplexity-only | `sources/__init__.py:28-36`, `waterfall_config.yaml:80` |
| 6 | `openai`/`anthropic` appear in **no** config file, so they cannot be registered even after #188 | both YAMLs |
| 7 | `EnrichRequest.object_type` (`"Account"`, `"res.partner"`) never matches waterfall domain keys (`company`/`contact`/`opportunity`) — a mapping layer is required | `models/schemas.py:41-44` vs `waterfall_config.yaml:4,40,76` |
| 8 | `quality_thresholds.yaml` nests `required_fields` under `per_domain_thresholds`, but the reader expects it top-level — so `required_fields` and `validation_rules` are **silently inert** and accuracy always returns the neutral 0.7 | `quality_thresholds.yaml:17-67` vs `quality_scorer.py:71-73,145-147` |
| 9 | Clearbit would build `.../v2/v2/companies/find` if fed `enrichment_sources.yaml` | `clearbit.py:69` vs `enrichment_sources.yaml:4` |
| 10 | No feature flag, no construction site, and **no `handle_enrich_consensus`** — despite four documents describing it as shipped | `config.py`, `main.py:58-126`, `handlers.py:418` |
| 11 | Per-source `priority`, `timeout`, `retry`, `cost`, `quality_threshold` and the `fields` allowlist in `waterfall_config.yaml` are **all inert** — only `name` is ever read | `waterfall_engine.py:279` |
| 12 | `fallback_behavior.on_quality_below_threshold` is compared against `"use_inference_bridge"` but ships as `retry_with_next_source`, so the branch is dead — and if it did match it only emits a log line | `waterfall_engine.py:258` vs `waterfall_config.yaml:98` |

Three config files describe enrichment sources in three mutually incompatible
shapes, and two of them have zero readers. Reconciling them onto one schema is
the first real task; the tests in `tests/test_waterfall_autoregister.py:18-39`
are the closest thing to a spec for the shape the loader expects.

Blockers 1-4 and 7 must be fixed before activation is even testable. Activation
itself changes enrichment behaviour for every request, including the live
Perplexity one, so it needs its own PR and review.

**Docs to correct alongside:** `app/services/enrichment/README.md:261`,
`TODO.md:338`, `workflow_state.md:47,89` and
`reports/GMP-Report-ENRICH-001-*.md:110-129` all describe a
`handle_enrich_consensus` handler. It does not exist — `handlers.py:418` and
`orchestration_layer.py:40-46` register enrich / enrichbatch / converge /
discover / simulate / writeback / enrich-and-sync only, and
`NodeRuntimeConfig.allowed_actions` (`main.py:153-164`) has no consensus action.

**Also still open:** `consensus_engine.py` has no provider dispatch, so
multi-variation consensus across providers remains unbuilt.

### Waterfall path does not unwrap the build_prompt envelope
- [x] `enrichment/sources/perplexity_adapter.py`, `enrichment/waterfall_engine.py`

**Resolved.** `prompt_builder` instructs the model to answer with an envelope,
`{"confidence": <float>, "fields": {...}}`. Two consumers stripped it and two
did not:

| Consumer | Before | Reachable from `app/` |
|---|---|---|
| `validation_engine.validate_response` | unwrapped | yes — `enrichment_orchestrator`, the live path |
| `simulation_bridge._sonar_entity_for_name` | unwrapped | yes — via `engines/handlers.py` |
| `perplexity_adapter` | **not** unwrapped | no — `WaterfallEngine` only |
| `waterfall_engine` consensus variations | **not** unwrapped | no — `WaterfallEngine` only |

**This was never a production defect**, and an earlier revision of this entry
wrongly said it was. Every reachable consumer already unwrapped; both broken
consumers sat behind `WaterfallEngine`, which nothing under `app/` constructs.
The claim came from grepping `json.loads` without following the call path —
the same mistake this file records against the provider-adapter entry above.

Fixed anyway, because the waterfall path must be correct *before* anyone
activates it: an un-unwrapped source merges the two wrapper keys and scores a
completeness of 1.0 over an empty payload, which is high enough to stop the
waterfall from trying another source.

`unwrap_envelope()` now lives in `prompt_builder.py`, beside the prompt that
creates the envelope, so a new consumer does not have to rediscover the shape.
`validate_response` and `simulation_bridge` keep their own inline unwraps —
both work, both are on live paths, and rewriting them would be risk without
behavioural gain.


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
### N-ary inference enablement plan (deferred, not started)

`app/engines/inference/nary_inference_engine.py` (148 stmts) is **kept and
declared staged** in `tests/compliance/test_module_reachability.py::STAGED_ARTIFACTS`.
It is not dead code and it is not wired — it needs a feature project, not a
wiring change. Two things block it, both verified on 2026-08-23:

**Its bridge targets a protocol that does not exist.** `to_rule_engine_format()`
returns `{relation, entities, confidence, source, rule, provenance,
qualifier_trace, explanation}`. But `inference/rule_engine.InferenceResult` is
`{derived_fields, rules_fired, rules_evaluated, rules_skipped,
derivation_chains, inference_confidence, cascade_depth}`, and
`inference_bridge_v2.InferenceResult` is a third shape, `{derived, blocked}`
over `FieldInferenceResult`. **Zero field overlap with either.** The docstring's
claim of compatibility was never true against the consumers it names.

**There is no n-ary data.** The entire KB is `kb/plastics_recycling.yaml`, whose
only top-level key is `inference_rules`. `NAryFact` requires `relation` +
`participants: {role -> entity_id}` + `qualifiers`. Nothing in the KB has that
shape, and `load_kb_facts()` takes pre-built objects — there is no loader.

Prerequisites, in order. The first is not an engineering task:

1. [ ] **Author n-ary facts for the plastics domain.** Domain modelling; needs
       someone with the domain, not a code change.
2. [ ] **Design the KB n-ary fact schema** and extend the `kb/*.yaml` contract.
3. [ ] **Write the YAML -> `NAryFact` loader.**
4. [ ] **Choose the target result shape** and reconcile the three above. Pick
       one before writing any bridge code.
5. [ ] **Add the call site** in `inference_bridge_v2` (which today contains no
       n-ary references at all) and cover `combined_infer()`.

Until step 1 exists, steps 2-5 have nothing to operate on. Deleting the module
instead is defensible — it is reversible from git — but it is the only
surviving implementation of n-ary inference, so it is staged rather than cut.

- [x] **Coverage floor raised to 71%** (was 60%, then 68%). All seven in-tree
      sites are synced — `pytest.ini`, `pyproject.toml`, `ci.yml`,
      `pr-pipeline.yml`, `refactoring-validation.yml`, `Makefile`,
      `.github/env.template` — plus contract C-15, INV-9 and the six other
      living docs. `pyproject.toml` was previously missed because it spells the
      setting `fail_under`, not `cov-fail-under`; it sat at 60 and would have
      silently become the floor had `.coveragerc` ever been removed.
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
