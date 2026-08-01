# Workflow State — Enrichment.Inference.Engine

**Last Updated:** 2026-06-24
**Current Phase:** PHASE 4 — Validation (L9_CANON Gap Closure)

---

## Context Summary

The Enrichment.Inference.Engine is the Layer 2 ENRICH service in the L9 constellation. It provides:

- Multi-pass convergence loop for AI-powered entity enrichment
- Consensus-based multi-variation LLM synthesis
- Domain KB injection for vertical-specific context
- Inference bridge for deterministic rule firing
- Graph sync for pushing enriched entities to Neo4j

**Recent Major Milestone:** Transport SDK cutover completed for the live node runtime — SDK now owns `/v1/execute`, handlers register through the SDK registry, and Gate-only egress replaces direct peer transport in the active runtime path.

### Standalone Test Harness (No Gate)

`plastics_enrichment_client.py` at repo root is **intentionally kept** as a manual integration-test script — not production code.

| Item | Detail |
| ---- | ------ |
| **Purpose** | Exercise enrichment against [Enrichment.Inference.Engine](https://github.com/cryptoxdog/Enrichment.Inference.Engine) + [IB-Odoo_19](https://github.com/cryptoxdog/IB-Odoo_19) without routing through Gate |
| **When to use** | Local/dev smoke tests, Odoo-side validation, plastics multi-pass Perplexity prompt experiments |
| **Not for** | Production dispatch, CI, SDK `/v1/execute`, or Gate-authored transport |
| **How it runs** | Direct `perplexity-sdk` calls + YAML super-prompt config (`plastics_recycling_super_prompt.yaml`); `python plastics_enrichment_client.py` from repo root |
| **Production path** | `app/services/enrichment/waterfall_engine.py`, `app/engines/enrichment_orchestrator.py`, SDK runtime + Gate egress |

Do not delete this file without confirming Odoo-side test workflows no longer depend on it.

---

## Current State

### What's Built and Working

| Component | Status | Files |
| --------- | ------ | ----- |
| Core Enrichment Pipeline | ✅ Production-ready | `waterfall_engine.py`, `enrichment_orchestrator.py` |
| Consensus Engine | ✅ Enhanced | `consensus.py` with `synthesize()`, `merge_with_priority()` |
| Uncertainty Engine | ✅ Enhanced | `uncertainty.py` with `apply_uncertainty()`, risk levels |
| KB Resolver | ✅ Enhanced | `kb_resolver.py` with `KBContext`, domain fragments |
| Prompt Variations | ✅ New | `build_variation_prompts()` in `prompt_builder.py` |
| Consensus Handler | ✅ New | `handle_enrich_consensus` in `handlers.py` |
| HEALTH Service | ✅ Complete | 5 files in `app/health/` |
| SCORE Service | ✅ Complete | 6 files in `app/score/` |
| CI/CD Pipelines | ✅ Complete | 13 workflow files in `.github/workflows/` |
| Test Coverage | ✅ Expanded | 54 test files (was 1) |
| SDK Node Runtime | ✅ Active | `app/main.py`, `app/engines/orchestration_layer.py`, `app/api/v1/chassis_endpoint.py` |

### Gap Analysis Status (from Core Gap Analysis documents)

| Category | Total Gaps | Closed | Remaining |
| -------- | ---------- | ------ | --------- |
| ENRICH Engine Internal | 8 | 5 | 3 |
| Convergence Loop Structural | 4 | 2 | 2 |
| GRAPH Engine Service | 5 | 1 | 4 |
| ENRICH ↔ GRAPH Integration | 5 | 1 | 4 |
| Downstream Services | 6 | 2 | 4 |
| Infrastructure | 6 | 2 | 4 |
| Testing | 4 | 2 | 2 |
| **TOTAL** | **38** | **15** | **23** |

**Overall Progress:** ~40% of identified gaps closed

### Transport SDK Cutover Status (2026-04-11)

**Status:** ✅ LIVE RUNTIME CUTOVER COMPLETE

The repo now runs on the `constellation-node-sdk` transport surface:

- `app/main.py` creates the SDK node runtime via `create_node_app(...)`
- `/v1/execute` is SDK-owned; `app/api/v1/chassis_endpoint.py` keeps only app-specific supplemental routes
- runtime actions register through `constellation_node_sdk.runtime.handlers.register_handler`
- Gate-only egress is active in `app/engines/graph_sync_client.py`, `app/engines/packet_router.py`, and `app/services/graph_sync_hooks.py`
- local `chassis/` transport files and `app/engines/chassis_contract.py` were removed from the active codebase
- TransportPacket tests replaced the old app-local packet runtime slice

---

## Decision Log

| Date | Decision | Rationale |
| ---- | -------- | --------- |
| 2026-03-30 | Merge Enrichment-Deployment-pack into WaterfallEngine | L9 architecture compliance — single chassis ingress |
| 2026-03-30 | Add consensus-mode enrichment via `handle_enrich_consensus` | Enables multi-variation LLM synthesis with field-level voting |
| 2026-03-30 | Create `KBContext` dataclass for KB resolution | Structured context injection for domain-specific prompts |
| 2026-06-24 | Retain `plastics_enrichment_client.py` at repo root | Standalone Gate-bypass harness for Engine + IB-Odoo_19 integration testing only |

---

## Open Questions

1. **PostgreSQL persistence strategy** — Use SQLAlchemy async or raw asyncpg?
2. **Event bus choice** — Redis Streams vs NATS for inter-service events?
3. **Multi-provider LLM priority** — OpenAI first or Anthropic for consensus diversity?

---

## Test Status

```text
Full suite:       1142 passing, 108 failing (pre-existing), 23 errors (pre-existing)
New/fixed tests:    39 passing, 1 skipped (OTEL not installed)
```

Last run: 2026-04-11

### New test files created

- `tests/services/test_event_emitter.py` — 10 tests (event serialization, emit, timeout, error swallow)
- `tests/services/test_chassis_handlers.py` — 8 tests (community export, schema proposal)
- `tests/test_telemetry.py` — 2 tests (OTEL instrumentation; skipped when OTEL not installed)
- `tests/integration/test_convergence_e2e.py` — 4 tests (full convergence loop with mocked LLM boundary)

### Fixed test files

- `tests/integration/test_gap_fixes.py` — Rewrote stale tests to match current APIs (ResultStore, Settings, converge endpoint). 17 passing.

### Pre-existing failures (not caused by this session)

- `tests/test_validation_engine.py` — API mismatch (dict vs object)
- `tests/test_kb_resolver.py` — Missing `kb_dir` arg
- `tests/test_loop_state.py` — Abstract class instantiation
- `tests/test_pplx_research.py` — `PerplexityClient` not defined
- `tests/integration/test_converge_api.py` — OTEL not installed
- `tests/contracts/tier2/` — 8 enforcement/contract test failures

---

## Next Steps

**Immediate priorities:**

1. Fix pre-existing stale test files (`test_validation_engine.py`, `test_kb_resolver.py`, `test_loop_state.py`, `test_pplx_research.py`) to match current APIs.
2. Install `opentelemetry` packages locally to enable telemetry tests and `test_converge_api.py`.
3. Add async timeout wrappers to remaining I/O calls in `loop_state.py`, `converge.py`, `pg_store.py`.
4. Remove or deprecate legacy direct-peer config fields once downstream services no longer rely on them.
5. Address remaining 23 platform gaps from Core Gap Analysis (GRAPH integration, infrastructure, downstream services).

---

## Recent Sessions (7-day window)

- 2026-06-24: Documented `plastics_enrichment_client.py` as intentional Gate-bypass test harness for Engine ↔ IB-Odoo_19 integration (not production path)
- 2026-04-11: L9_CANON gap closure — structlog conversion (9 files), async timeouts + error handling (event_emitter, handlers, pg_store), typing tightened, 4 new test files (39 tests), test_gap_fixes.py rewritten to match current APIs, chassis_handlers structlog bug fixed
- ✅ 2026-04-11: Transport SDK cutover — SDK runtime adopted in `app/main.py`, Gate-only egress wired, local `chassis/` files removed, targeted transport slice green, broader transport-adjacent slice at 126 passing with 8 legacy `test_gap_fixes` failures remaining
- 2026-04-07: Gap analysis of `gap-fixes/` vs `app/` — 10 gap fixes identified as NOT integrated, blocked on SDK chassis
- 2026-03-30: GMP-ENRICH-001 — Consensus-mode enrichment merge (14 TODOs completed)
- 2026-03-30: Gap analysis audit — 4 reference documents analyzed, status report generated
