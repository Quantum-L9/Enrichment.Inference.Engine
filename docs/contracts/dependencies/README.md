# Dependency Contracts

> All external and internal service dependencies of the Enrichment Inference Engine.

## Dependency Map

| Service | Type | Required | Auth | Purpose |
|---------|------|----------|------|---------|
| Perplexity Sonar | External | ✅ YES | Bearer key | Primary LLM enrichment |
| OpenAI | External | Optional | Bearer key | Secondary LLM |
| Anthropic Claude | External | Optional | Bearer key | Secondary LLM |
| Clearbit | External | Optional | API key | Waterfall enrichment |
| ZoomInfo | External | Optional | API key | Waterfall enrichment |
| Apollo | External | Optional | API key | Waterfall enrichment |
| Hunter | External | Optional | API key | Email enrichment |
| Odoo CRM | External | Optional | Username+pass | CRM consumer #1 |
| Salesforce | External | Optional | OAuth2 | CRM consumer #2 |
| HubSpot | External | Optional | Bearer token | CRM consumer #3 |
| Redis | Internal | Optional | None | Idempotency + events |
| PostgreSQL | Internal | Optional | Connection URL | Persistence |
| Constellation Gate | Internal | Required | Packet signature (`L9_SIGNING_KEY`) | Sole route to CEG (`sync`, `outcomes`) and ingress for `enrich`/`converge` |

