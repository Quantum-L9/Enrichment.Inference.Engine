# Architecture — Enrichment.Inference.Engine

## System Purpose

Universal domain-aware entity enrichment API for the L9 Constellation.
Enriches CRM entity records (Odoo + Salesforce) with structured intelligence extracted from external sources via LLM inference, normalizes data, and syncs to Neo4j for graph-based matching.

---

## Technology Stack

| Layer | Technology |
|-------|------------|
| Runtime | Python 3.12+ |
| API Framework | FastAPI 0.115+ |
| Production Transport Ingress | constellation_node_sdk runtime |
| LLM Inference | Perplexity sonar-reasoning |
| Knowledge Graph | Neo4j 5.x |
| Cache | Redis 7 |
| State Store | PostgreSQL 16 |
| CRM Integration | Odoo 19 (PlasticOS) + Salesforce |
| Observability | structlog + OpenTelemetry |
| Config | pydantic-settings v2 |

---

## Component Map

```text
┌─────────────────────────────────────────────────────────────────┐
│  API Surface (app/main.py + app/api/)                          │
│  FastAPI routes + mounted routers + supplemental HTTP routes   │
│  `/api/v1/*`, `/v1/outcomes`                                   │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│  SDK Transport Runtime                                          │
│  `/v1/execute` owned by create_node_app(...)                    │
│  allowed_actions + lifecycle registration + handler dispatch    │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│  Engine Layer (app/engines/)                                    │
│  ├── handlers.py              (action handlers)                 │
│  ├── orchestration_layer.py   (canonical SDK registration)      │
│  ├── enrichment_orchestrator.py                                 │
│  ├── convergence_controller.py                                  │
│  │     ├── confidence_tracker.py                                │
│  │     ├── cost_tracker.py                                      │
│  │     └── loop_state.py                                        │
│  ├── inference_bridge_v2.py   (DAG-based inference)             │
│  ├── meta_prompt_planner.py                                     │
│  └── graph_sync_client.py     (Gate transport)                  │
└──────────────────────────────┬──────────────────────────────────┘
                               │
              ┌────────────────┴────────────────┐
              ▼                                 ▼
┌─────────────────────────┐       ┌─────────────────────────────┐
│  Inference Engine       │       │  External Services          │
│  (app/engines/inference)│       │  tools/pplx_research.py     │
│  grade_engine.py        │       │  (Perplexity sonar)         │
│  rule_engine.py         │       └─────────────────────────────┘
│  nary_inference.py      │
└─────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Storage Layer                                                  │
│  PostgreSQL (state) │ Neo4j (graph) │ Redis (cache)             │
└─────────────────────────────────────────────────────────────────┘
```

---

## Data Flow — Enrichment Run

```text
1. API receives an enrichment request via `/api/v1/*` OR SDK runtime receives `/v1/execute`
2. SDK runtime dispatches the allowed action to registered handlers
3. EntropyEngine scores data completeness (1–5)
4. KBResolver adds domain-relevant KB context to prompts
5. Convergence loop runs multi-pass enrichment:
   a. Meta-prompt planner selects fields to enrich
   b. Perplexity queries execute (concurrent, rate-limited)
   c. Inference bridge fires deterministic rules
   d. Confidence tracker updates per-field scores
6. Graph sync pushes enriched entity to Neo4j via Gate transport
7. Response is returned through the runtime/app surface
```

---

## Key Contracts

| Contract               | Location                                                        | Enforced By                              |
| ---------------------- | --------------------------------------------------------------- | ---------------------------------------- |
| SDK transport dispatch | `app/main.py`, SDK runtime, registered handlers                 | Runtime bootstrap + handler registration |
| Handler signature      | `app/engines/handlers.py`, `app/engines/orchestration_layer.py` | C-02 contract                            |
| Tenant isolation       | All Neo4j queries                                               | `WHERE n.tenant_id = $tenant`            |
| Graph/Gate transport   | `app/engines/graph_sync_client.py`                              | Gate SDK                                 |
| Coverage minimum       | CI pipeline                                                     | 60% coverage gate                        |

---

## Boundary Rules

1. **SDK runtime owns `/v1/execute`** — local deprecated chassis router must not be used for production ingress
2. **No FastAPI in engine-only modules** — chassis/API isolation (C-01)
3. **No business logic in app/api/** — routes delegate to engines/services only
4. **No direct DB writes from API layer** — always through service/engine layer
5. **No LLM calls outside designated clients** — single point of control
6. **No raw HTTP peer-to-peer node calls** — inter-node communication routes through Gate/SDK transport

---

## Active Transport Bundle

These files define the live production transport/runtime contract and must remain aligned:

* `app/main.py`
* `app/api/v1/chassis_endpoint.py`
* `app/services/chassis_handlers.py`
* `app/engines/orchestration_layer.py`
* `app/engines/handlers.py`
* `app/engines/graph_sync_client.py`

---

## Deprecated Runtime Paths

These files are deprecated compatibility artifacts and are not the constitutional source of production transport behavior:

* `chassis/envelope.py`
* `chassis/router.py`
* `chassis/registry.py`

They may remain temporarily for migration safety, historical reference, or isolated compatibility use. Do not route production ingress or handler dispatch through them.

---

## Module Ownership

| Directory / File                     | Owner          | Tier |
| ------------------------------------ | -------------- | ---- |
| `app/api/`                           | API team       | T3   |
| `app/engines/`                       | Engine team    | T3   |
| `app/main.py`                        | Platform       | T4   |
| `app/api/v1/chassis_endpoint.py`     | Platform       | T4   |
| `app/services/chassis_handlers.py`   | Platform       | T4   |
| `app/engines/orchestration_layer.py` | Platform       | T4   |
| `app/engines/handlers.py`            | Platform       | T4   |
| `app/engines/graph_sync_client.py`   | Platform       | T4   |
| `app/models/`                        | Schema team    | T5   |
| `kb/`                                | Knowledge team | T5   |

---

## Related Documents

* `AGENTS.md` — Agent operating model and contracts
* `AI_AGENT_REVIEW_CHECKLIST.md` — PR review protocol
* `REPO_MAP.md` — Directory ownership and runtime boundaries
* `EXECUTION_FLOWS.md` — Initialization and execution paths
* `INVARIANTS.md` — Immutable repository rules
