# ═══════════════════════════════════════════════════════════════
# Contract: Versioning Policy
# Source:   docs/contracts/VERSIONING.md
# Version:  1.0.0
# Updated:  2026-04-05
# ═══════════════════════════════════════════════════════════════

# Versioning Policy

> How contract versions are assigned, incremented, and communicated.

## Semantic Versioning

All contracts use **SemVer 2.0.0** (`MAJOR.MINOR.PATCH`):

| Change Type | Version Bump | Examples |
|-------------|-------------|---------|
| Breaking schema change | MAJOR | Remove field, change required→optional, rename field |
| Backward-compatible addition | MINOR | Add optional field, add new endpoint |
| Documentation/description fix | PATCH | Fix typo, clarify description, add example |

## Current Contract Versions

| Contract File | Version | Last Changed |
|---------------|---------|-------------|
| `api/openapi.yaml` | 2.5.0 | 2026-11-13 |
| `agents/tool-schemas/*` | 1.0.0 | 2026-04-05 |
| `agents/protocols/packet-envelope.yaml` | 1.0.0 | 2026-04-05 |
| `data/models/*` | 1.0.0 | 2026-04-05 |
| `data/graph-schema.yaml` | 2.1.0 | 2026-11-11 |
| `events/asyncapi.yaml` | 1.0.0 | 2026-04-05 |
| `config/env-contract.yaml` | 2.3.0 | 2026-04-05 |

## API Version Alignment

The API contract version (`openapi.yaml`) tracks the application version declared in:
- `app/main.py`: `version="2.3.0"`
- `app/models/schemas.py`: `inference_version: str = "v2.2.0"` (internal inference version, increments independently)

## Changelog Format

Every MAJOR or MINOR bump must be recorded in `CHANGELOG.md` at the repo root:

```markdown
## [2.4.0] — YYYY-MM-DD
### Added
- POST /v1/new-endpoint: ...

## [2.3.0] — 2026-04-05
### Added
- POST /v1/converge/batch: batch convergence loop
- GET /api/v1/fields/{entity_id}/{field}/history: time-series confidence
### Changed
- EnrichResponse: added `pass_count`, `uncertainty_score`, `quality_tier`
```

## Breaking Change Protocol

1. Increment MAJOR in the contract file header
2. Add `x-deprecated: true` + `x-sunset-date` to the old operation in OpenAPI
3. Update `CHANGELOG.md`
4. Notify all registered consumers (Salesforce package, Odoo bridge, any L9 nodes)
5. Keep old endpoint live for ≥ 30 days after `x-sunset-date`
