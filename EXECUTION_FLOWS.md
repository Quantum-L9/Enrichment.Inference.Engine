# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: enrichment
# layer: [governance]
# tags: [L9_TEMPLATE, execution, flows, runtime]
# owner: platform
# status: active
# token_estimate: 825
# ssot_for: [runtime-flows, initialization-sequence, failure-flows]
# load_when: [control_flow_change, runtime_question, error_flow_question]
# references: [AGENTS.md, CONFIG_ENV_CONTRACT.md, REPO_MAP.md]
# --- /L9_META ---

# EXECUTION_FLOWS.md — Runtime Execution Paths

**VERSION**: 2.1.0 | **SHA_BASELINE**: 358d15d | **LAST_REVIEWED**: 2026-04-11

Entry points cross-reference REPO_MAP.md. Tenant resolution: AGENTS.md C-03. Circuit breaker env vars: CONFIG_ENV_CONTRACT.md.

## Initialization Sequence (9 Steps)

| Step | Action | Failure Behavior |
|---|---|---|
| 1 | pydantic-settings loads Settings from env | Raises ValidationError — app does NOT start |
| 2 | Logging configured via `setup_logging()` | Fatal — app does NOT start |
| 3 | KBResolver initialized | Fatal if required KB path/config is unusable |
| 4 | Idempotency store attempts Redis connection | Warns and degrades if unavailable |
| 5 | SDK runtime handler registry cleared | Fatal if runtime bootstrap fails |
| 6 | Canonical orchestration handlers registered via `app/engines/orchestration_layer.py` | Fatal if handler registration fails |
| 7 | Supplemental handlers registered via `app/services/chassis_handlers.py` | Fatal if registration fails |
| 8 | Persistence/event systems initialized and converge module configured | Fatal on unrecoverable store/runtime init failure |
| 9 | `create_node_app(...)` serves SDK transport ingress and mounted app routes | Fatal if app assembly fails |

Health check readiness is determined by successful runtime/app initialization and downstream service state.

---

## Transport Execution Flow

Production transport ingress is SDK-owned.

```text
Client / caller
  -> POST /v1/execute
  -> SDK runtime receives packet/request
  -> runtime validates allowed action
  -> registered handler executes
  -> response returned through runtime
```

Canonical runtime ownership:

* `/v1/execute` is owned by `create_node_app(...)` in `app/main.py`
* handler registration happens during lifecycle startup
* transport-adjacent app routes remain in `app/api/v1/chassis_endpoint.py`

Deprecated transport flow:

* `chassis/envelope.py`
* `chassis/router.py`
* `chassis/registry.py`

These are not the production execution path.

---

## API Request Flow

```text
HTTP request
  -> app/main.py / mounted router
  -> route dependency/auth checks
  -> engine/service delegation
  -> response model / response dict
```

Examples:

* `POST /api/v1/enrich`
* `POST /api/v1/enrich/batch`
* `GET /api/v1/health`
* `POST /v1/outcomes`

---

## Tenant Resolution Waterfall

Resolved in order — first match wins:

1. HTTP header `X-Tenant-ID`
2. Subdomain extraction
3. API key mapping
4. transport payload / runtime context tenant value
5. default tenant fallback

If no level resolves: return failure per route/runtime contract.

---

## Failure Flows

### Redis Unavailable

1. Redis-backed idempotency store init fails
2. App logs warning
3. Runtime continues with degraded capability where supported
4. Features depending on Redis-backed persistence may lose durability/optimization but must fail safely

### Graph / Gate Transport Failure

1. Outbound Gate transport call fails
2. Client logs structured error
3. Caller receives safe failure status or degraded response path
4. No raw peer-to-peer HTTP fallback is introduced

### Startup Validation Failure

1. Required config missing or invalid
2. App bootstrap fails
3. Service does not start

---

## GDS Scheduler Flow

The GDS scheduler is spec-driven. No hardcoded algorithm execution logic exists outside the scheduler.

Flow:

* domain spec loaded
* `gds_jobs` parsed
* scheduler validates job spec
* job executed via graph layer
* result recorded/logged

---

## Known Documentation Gaps

| Flow                          | Status                               | Tracking              |
| ----------------------------- | ------------------------------------ | --------------------- |
| Neo4j internal query patterns | Unknown — no exhaustive doc coverage | Issue label: docs-gap |
| PostgreSQL access layer       | Unknown                              | Issue label: docs-gap |
| KGE training flow             | Unknown — implementation thin        | Issue label: docs-gap |

Agents encountering an unknown flow: log, annotate, and do not invent runtime behavior.
