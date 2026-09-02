# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: enrichment
# layer: [governance]
# tags: [L9_TEMPLATE, config, env-vars, contract]
# owner: platform
# status: active
# token_estimate: 1200
# ssot_for: [environment-variables-human-summary]
# load_when: [env_var_change, config_question, startup_failure]
# references: [AGENTS.md, INVARIANTS.md INV-20, docs/contracts/config/env-contract.yaml, .env.example]
# --- /L9_META ---

# CONFIG_ENV_CONTRACT.md — Environment Variables Contract (summary)

**VERSION**: 2.1.0 | **SHA_BASELINE**: 358d15d | **LAST_REVIEWED**: 2026-04-11

> **Machine SSOT:** [`docs/contracts/config/env-contract.yaml`](docs/contracts/config/env-contract.yaml) — full variable list, defaults, GitHub Actions mappings, and `required_for_*` checklists.
> **Human template:** [`.env.example`](.env.example) — copy to `.env` / `.env.local` (`.env.local` overrides and is gitignored).

`app/core/config.py` (`Settings`, pydantic-settings) loads **uppercase env names** derived from field names (e.g. `gate_url` → `GATE_URL`). There is **no** `L9_` prefix on database settings in `Settings`; the `L9_*` names below are read by the Gate SDK, not by `Settings`.

---

## Minimum .env for local enrichment

Typical values (align with `.env.example`):

```bash
PERPLEXITY_API_KEY=pplx-your-key-here
API_KEY_HASH=<sha256-hex-of-your-client-api-key>
REDIS_URL=redis://localhost:6379/0
DATABASE_URL=postgresql+asyncpg://enrich:changeme@localhost:5432/enrich
GATE_URL=http://localhost:8080
L9_NODE_NAME=enrichment-engine
L9_ENVIRONMENT=local
```

`API_SECRET_KEY` exists on `Settings` but is **not** used for client auth; `X-API-Key` is verified against `API_KEY_HASH` (see `app/core/auth.py`).

---

## Core variables (Settings)

| Variable | Role |
|----------|------|
| `PERPLEXITY_API_KEY` | Sonar / enrichment provider |
| `PERPLEXITY_MODEL` | Model id (default `sonar-reasoning`) |
| `API_KEY_HASH` | SHA-256 hex of accepted API key |
| `API_SECRET_KEY` | Optional reserved field (see yaml contract) |
| `REDIS_URL` | Idempotency / rate limiting |
| `DATABASE_URL` | Async SQLAlchemy URL (`asyncpg`) |
| `KB_DIR`, `DOMAINS_DIR`, `DEFAULT_DOMAIN` | KB and domain packs |
| `GATE_URL`, `GATE_REGISTRATION_ENABLED`, `GATE_INTERNAL_URL`, `GATE_ADMIN_TOKEN` | Constellation Gate — the only peer transport (no peer URLs) |
| `L9_NODE_NAME`, `L9_ENVIRONMENT`, `L9_REQUIRE_SIGNATURE`, `L9_SIGNING_KEY`, `L9_SIGNING_KEY_ID`, `L9_SIGNING_ALGORITHM`, `L9_VERIFYING_KEYS_JSON` | Gate SDK identity + packet signing (read by SDK) |
| `GRAPH_SYNC_ENTITY_TYPE`, `GRAPH_SYNC_ID_PROPERTY` | CEG `sync` row shape |
| `DEFAULT_CONSENSUS_THRESHOLD`, `DEFAULT_MAX_VARIATIONS`, `DEFAULT_TIMEOUT_SECONDS` | Enrichment defaults |
| `MAX_CONCURRENT_VARIATIONS`, `MAX_ENTITIES_PER_BATCH` | Concurrency / batch caps |
| `CB_FAILURE_THRESHOLD`, `CB_COOLDOWN_SECONDS` | Circuit breaker |
| `MAX_BUDGET_TOKENS`, `MAX_BUDGET_TOKENS_DEFAULT`, `TOKEN_RATE_USD_PER_1K` | Convergence cost |
| `LOG_LEVEL` | Logging (`Settings.log_level`) |

Docker Compose–only keys (`COMPOSE_PROJECT_NAME`, `POSTGRES_*`, `GRAFANA_*`, `APP_PORT`, …) are documented in `.env.example` and in `env-contract.yaml`.

---

## Outside Settings

OpenTelemetry and similar use standard `OTEL_*` / process env as described under `other_runtime_env` in [`env-contract.yaml`](docs/contracts/config/env-contract.yaml) (see `app/core/telemetry.py`).

---

## AWS Secrets Manager

**STATUS: ASPIRATIONAL — NOT IMPLEMENTED.**
Do not add AWS SDK calls expecting this path until an issue with label `infra-roadmap` is closed.

---

## Startup failures

On misconfiguration, pydantic-settings may raise `ValidationError` listing fields. Prefer fixing env values using **`.env.example`** and **`env-contract.yaml`** as references — not ad hoc renames (C-09 in [AGENTS.md](AGENTS.md) for *new* application-level vars: prefer `L9_` unless infrastructure-standard).
