# docs/contracts/

> Complete contract registry for the **L9 Enrichment Inference Engine** (`cryptoxdog/Enrichment.Inference.Engine`).
> Every contract traces directly to source code. Nothing is fabricated.

---

## Architecture Summary

The **Enrichment Inference Engine** is a **Hybrid** (AI Agent Framework + Data Pipeline + Monolith) system serving as **Layer 2** of the L9 three-layer intelligence stack. It exposes a FastAPI service (`app/main.py`) with a `PacketEnvelope` chassis protocol for node-to-node routing, a multi-source waterfall enrichment pipeline, a multi-pass convergence loop, deterministic inference rules, multi-dimensional scoring, CRM writeback, and an MCP (Model Context Protocol) server for AI agent integration.

**Architecture archetypes:** AI Agent Framework × Data Pipeline × Monolith (single deployable)

**Node identity:** `enrichment-engine` (constellation node, L9 architecture)

**Adjacent nodes:** `graph-node` (CEG), `score-node`, `route-node`

---

## Contracts Index

| Contract | Type | Source File | Description |
|----------|------|-------------|-------------|
| `POST /api/v1/enrich` | REST API | `app/main.py:95` | Single entity enrichment |
| `POST /api/v1/enrich/batch` | REST API | `app/main.py:106` | Batch enrichment (≤50) |
| `GET /api/v1/health` | REST API | `app/main.py:81` | Health + KB + circuit breaker |
| `POST /v1/execute` | REST API | `app/api/v1/chassis_endpoint.py:38` | L9 PacketEnvelope chassis ingress |
| `POST /v1/converge` | REST API | `app/api/v1/converge.py` | Single entity convergence loop |
| `POST /v1/converge/batch` | REST API | `app/api/v1/converge.py` | Batch convergence |
| `GET /v1/converge/{run_id}` | REST API | `app/api/v1/converge.py` | Convergence loop status |
| `POST /v1/converge/{run_id}/approve` | REST API | `app/api/v1/converge.py` | Human approval for schema proposals |
| `GET /v1/converge/proposals/{domain}` | REST API | `app/api/v1/converge.py` | Pending schema proposals |
| `POST /api/v1/discover` | REST API | `app/api/v1/discover.py:51` | Schema discovery trigger |
| `POST /api/v1/scan` | REST API | `app/api/v1/discover.py:72` | CRM field scan (Seed tier) |
| `GET /api/v1/proposals/{domain}` | REST API | `app/api/v1/discover.py:93` | Get pending schema proposals |
| `POST /api/v1/proposals/{id}/approve` | REST API | `app/api/v1/discover.py:113` | Approve/reject proposal |
| `GET /api/v1/fields/{entity_id}` | REST API | `app/api/v1/fields.py:60` | Field confidence map |
| `GET /api/v1/fields/{entity_id}/{field}/history` | REST API | `app/api/v1/fields.py` | Field confidence time-series |
| `POST /v1/score/entity` | REST API | `app/score/score_api.py` | Score single entity |
| `POST /v1/score/batch` | REST API | `app/score/score_api.py` | Batch score entities |
| `GET /v1/score/{entity_id}` | REST API | `app/score/score_api.py` | Get latest score |
| `POST /v1/score/decay` | REST API | `app/score/score_api.py` | Apply temporal decay |
| `GET /v1/score/{entity_id}/explain` | REST API | `app/score/score_api.py` | Score explainability |
| `POST /v1/health/assess` | REST API | `app/health/health_api.py` | Full CRM health assessment |
| `GET /v1/health/ai-readiness` | REST API | `app/health/health_api.py` | AI readiness score |
| `enrich_contact` | MCP Tool | `app/agents/mcp_server.py:35` | Waterfall entity enrichment |
| `lead_router` | MCP Tool | `app/agents/mcp_server.py:46` | Lead routing via scores |
| `deal_risk` | MCP Tool | `app/agents/mcp_server.py:55` | Opportunity risk assessment |
| `data_hygiene` | MCP Tool | `app/agents/mcp_server.py:64` | CRM data quality assessment |
| `writeback` | MCP Tool | `app/agents/mcp_server.py:73` | CRM enrichment writeback |
| `EnrichmentResult` | Data Model | `app/services/pg_models.py:43` | Persisted enrichment output |
| `ConvergenceRun` | Data Model | `app/services/pg_models.py:93` | Multi-pass loop state |
| `FieldConfidenceHistory` | Data Model | `app/services/pg_models.py:137` | Per-field confidence time-series |
| `SchemaProposalRecord` | Data Model | `app/services/pg_models.py:165` | Schema proposals + approval |
| `EnrichRequest` / `EnrichResponse` | Pydantic Schema | `app/models/schemas.py:26` | Primary enrichment wire contract |
| `EnrichmentEvent` | Event Schema | `app/services/event_emitter.py:32` | Redis Streams event envelope |
| `enrichment_completed` | Event Channel | `app/services/event_emitter.py:68` | Enrichment lifecycle event |
| `convergence_completed` | Event Channel | `app/services/event_emitter.py:80` | Convergence lifecycle event |
| `schema_proposed` | Event Channel | `app/services/event_emitter.py:91` | Schema proposal event |
| `PacketEnvelope` | Chassis Protocol | `chassis/envelope.py` | L9 node-to-node wire format |
| Environment Config | Config | `app/core/config.py` + `.env.example` | All env variables |

---

## Contract Dependency Graph

```mermaid
graph LR
    SF[Salesforce Apex] -->|POST /api/v1/enrich| EIE[enrichment-engine]
    ODOO[Odoo async_executor] -->|POST /api/v1/enrich| EIE
    CLAY[Clay Webhook] -->|POST /api/v1/enrich| EIE
    MCP_CLIENT[AI Agent / Odoo Assistant] -->|MCP tools/call| MCP[MCP Server]
    MCP -->|chassis dispatch| EIE

    EIE -->|PacketEnvelope /v1/execute| GRAPH[graph-node :8001]
    EIE -->|PacketEnvelope /v1/execute| SCORE[score-node :8002]
    EIE -->|PacketEnvelope /v1/execute| ROUTE[route-node :8003]

    EIE -->|XADD enrich:events:{tenant}| REDIS[(Redis Streams)]
    EIE -->|asyncpg| PG[(PostgreSQL)]
    EIE -->|TransportPacket sync/outcomes via Gate| CEG[Cognitive Engine Graphs]

    EIE -->|waterfall| PPLX[Perplexity Sonar]
    EIE -->|waterfall| CLEARBIT[Clearbit API]
    EIE -->|waterfall| ZOOMINFO[ZoomInfo API]
    EIE -->|waterfall| APOLLO[Apollo API]
    EIE -->|waterfall| HUNTER[Hunter API]
    EIE -->|LLM| OPENAI[OpenAI API]
    EIE -->|LLM| ANTHROPIC[Anthropic API]

    EIE -->|writeback| ODOO_WB[Odoo CRM]
    EIE -->|writeback| SF_WB[Salesforce CRM]
    EIE -->|writeback| HS_WB[HubSpot CRM]

    REDIS -->|stream consumer| SCORE
    REDIS -->|stream consumer| ROUTE
```

---

## Directory Structure

```
docs/contracts/
├── README.md                          ← This file
├── VERSIONING.md                      ← Versioning policy
├── api/
│   ├── README.md
│   ├── openapi.yaml                   ← Full OpenAPI 3.1 spec
│   ├── endpoints/
│   │   ├── enrichment.yaml
│   │   ├── convergence.yaml
│   │   ├── discover.yaml
│   │   ├── fields.yaml
│   │   ├── score.yaml
│   │   ├── health.yaml
│   │   └── chassis.yaml
│   └── schemas/
│       ├── shared-models.yaml
│       └── error-responses.yaml
├── agents/
│   ├── README.md
│   ├── tool-schemas/
│   │   ├── enrich-contact.schema.json
│   │   ├── lead-router.schema.json
│   │   ├── deal-risk.schema.json
│   │   ├── data-hygiene.schema.json
│   │   ├── writeback.schema.json
│   │   └── _index.yaml
│   ├── prompt-contracts/
│   │   └── _index.yaml
│   └── protocols/
│       ├── packet-envelope.yaml
│       └── _index.yaml
├── data/
│   ├── README.md
│   ├── models/
│   │   ├── enrichment-result.schema.json
│   │   ├── convergence-run.schema.json
│   │   ├── field-confidence-history.schema.json
│   │   ├── schema-proposal.schema.json
│   │   └── _index.yaml
│   ├── graph-schema.yaml
│   └── migrations/
│       └── migration-policy.md
├── events/
│   ├── README.md
│   ├── asyncapi.yaml
│   ├── channels/
│   │   ├── enrichment-events.yaml
│   │   └── _index.yaml
│   └── schemas/
│       └── event-envelope.yaml
├── config/
│   ├── README.md
│   └── env-contract.yaml
├── dependencies/
│   ├── README.md
│   ├── perplexity-sonar.yaml
│   ├── openai.yaml
│   ├── anthropic.yaml
│   ├── clearbit.yaml
│   ├── zoominfo.yaml
│   ├── apollo.yaml
│   ├── hunter.yaml
│   ├── odoo-crm.yaml
│   ├── salesforce-crm.yaml
│   ├── hubspot-crm.yaml
│   ├── redis.yaml
│   ├── postgresql.yaml
│   ├── constellation-gate.yaml
│   └── _index.yaml
└── _templates/
    ├── api-endpoint.template.yaml
    ├── tool-schema.template.json
    ├── prompt-contract.template.yaml
    ├── event-channel.template.yaml
    └── data-model.template.json
```

---

## Validation Commands

```bash
# Validate OpenAPI spec
npx @redocly/cli lint docs/contracts/api/openapi.yaml

# Validate AsyncAPI spec
npx @asyncapi/cli validate docs/contracts/events/asyncapi.yaml

# Validate JSON Schemas
npx ajv validate -s docs/contracts/data/models/*.schema.json -d tests/fixtures/*.json

# Validate agent tool schemas
for f in docs/contracts/agents/tool-schemas/*.schema.json; do
  npx ajv validate -s "$f" && echo "✓ $f"
done

# Check contract drift
python tools/verify_contracts.py --contracts docs/contracts/ --source app/
```

---

## Usage

1. **API consumers** → read `api/openapi.yaml` for the complete endpoint surface.
2. **AI agent builders** → read `agents/tool-schemas/` for MCP tool input schemas.
3. **Data engineers** → read `data/models/` for PostgreSQL table schemas.
4. **Event consumers** → read `events/asyncapi.yaml` for Redis Streams event shapes.
5. **Ops / DevOps** → read `config/env-contract.yaml` for all required env vars.
6. **Integration teams** → read `dependencies/` for each external service contract.

