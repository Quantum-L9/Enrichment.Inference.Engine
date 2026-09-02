# API Contracts

> Complete REST API surface for the L9 Enrichment Inference Engine.

## Base URL
- Local: `http://localhost:8000`
- Production: `https://enrich.yourdomain.com`

## Authentication
All endpoints except `GET /api/v1/health` require `X-API-Key` header.
Source: `app/core/auth.py` — SHA-256 constant-time comparison.

## Rate Limiting
120 requests/minute (global). Source: `app/middleware/rate_limiter.py`

## Endpoints Summary

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/v1/health` | None | Health + KB status |
| POST | `/api/v1/enrich` | ✅ | Enrich single entity |
| POST | `/api/v1/enrich/batch` | ✅ | Batch enrichment (≤50) |
| POST | `/v1/execute` | ✅ | Chassis PacketEnvelope |
| POST | `/v1/converge` | ✅ | Convergence loop |
| POST | `/v1/converge/batch` | ✅ | Batch convergence |
| GET | `/v1/converge/{run_id}` | ✅ | Loop status |
| POST | `/v1/converge/{run_id}/approve` | ✅ | Human approval |
| GET | `/v1/converge/proposals/{domain}` | ✅ | Pending proposals |
| POST | `/api/v1/discover` | ✅ | Schema discovery |
| POST | `/api/v1/scan` | ✅ | CRM field scan |
| GET | `/api/v1/proposals/{domain}` | ✅ | Schema proposals |
| POST | `/api/v1/proposals/{id}/approve` | ✅ | Approve/reject proposal |
| GET | `/api/v1/fields/{entity_id}` | ✅ | Field confidence map |
| GET | `/api/v1/fields/{entity_id}/{field}/history` | ✅ | Field history |

## Full Spec
See `openapi.yaml` for the complete OpenAPI 3.1.0 specification.

