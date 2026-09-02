# Configuration Contracts

> All environment variables consumed by the Enrichment Inference Engine.

## Configuration Loading

Variables are loaded once at startup via `pydantic-settings` `BaseSettings` with `@lru_cache`.
Source: `app/core/config.py:Settings`

File: `.env` (see `.env.example` at repo root)

## Required Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `PERPLEXITY_API_KEY` | **YES** | Primary enrichment source |
| `API_SECRET_KEY` | **YES** | API authentication |
| `DATABASE_URL` | Recommended | PostgreSQL persistence |
| `REDIS_URL` | Recommended | Idempotency + event streaming |
| `GATE_URL` | **YES** (staging/prod) | Constellation Gate — sole peer transport |
| `L9_SIGNING_KEY`, `L9_SIGNING_KEY_ID` | **YES** (staging/prod) | Gate packet signing |

All other variables have defaults and are optional.

