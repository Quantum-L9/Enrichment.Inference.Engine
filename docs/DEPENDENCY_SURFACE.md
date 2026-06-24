# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: enrichment
# layer: [governance]
# tags: [L9_TEMPLATE, dependencies, versions, imports]
# owner: platform
# status: active
# token_estimate: 1171
# ssot_for: [dependency-versions, upgrade-workflow]
# load_when: [dep_change, dep_update, security_review]
# references: [AGENT.md, pyproject.toml]
# --- /L9_META ---

# DEPENDENCY_SURFACE.md — External Dependencies Inventory

**VERSION**: 2.0.0 | **SHA_BASELINE**: 358d15d | **LAST_REVIEWED**: 2026-04-01

> SSOT for dependency versions and upgrade workflow.
> Import restriction rules (chassis boundary): see AGENT.md C-01 (SSOT — not here).

---

## Version Table

| Package | Version Constraint | Pin Strategy | Notes |
|---|---|---|---|
| fastapi | ^0.115 | Minor-pinned | Core framework — chassis only |
| pydantic | ^2.9 | Minor-pinned | v2 runtime imports required (TC001-TC003 suppressed) |
| pydantic-settings | ^2.6 | Minor-pinned | Env var loading |
| structlog | ^24.4 | Minor-pinned | Observability — mandatory |
| uvicorn | ^0.32 | Minor-pinned | ASGI server |
| redis | ^5.1 | Minor-pinned | Async client |
| httpx | ^0.27 | Minor-pinned | Perplexity API calls |
| pyyaml | ^6.0 | Minor-pinned | Domain spec loading (safe_load only) |
| sqlalchemy | ^2.0 | Minor-pinned | PostgreSQL ORM |
| alembic | ^1.13 | Minor-pinned | Migrations |
| neo4j | >=5.0,<6.0 | PINNED | Graph driver — see gap note below |

### Development Dependencies

| Package | Version | Notes |
|---|---|---|
| pytest | ^8.3 | Test runner |
| pytest-asyncio | ^0.24 | Async test support |
| pytest-cov | ^6.0 | Coverage |
| ruff | 0.8.6 | Pinned exactly — do not upgrade without ADR |
| mypy | ^1.13 | Type checker (non-blocking per WAIVER-001) |
| pre-commit | ^4.0 | Hook runner |

---

## Known Gaps

### Neo4j Driver (PRODUCTION RISK)
The `neo4j` package may not be explicitly declared in `pyproject.toml`. It is possibly pulled
transitively by another package at an unpinned version. **Action required**: Confirm explicit
pin in `pyproject.toml` as `neo4j>=5.0,<6.0`. Until confirmed, this is a production risk.
Track: GitHub issue label `docs-gap`.

---

## Dependency Upgrade Workflow

Agents asked to upgrade a dependency MUST follow this sequence:

1. Update version constraint in `pyproject.toml` only.
2. Run `pip install -e ".[dev]"` to resolve.
3. Check dep changelog for breaking changes against codebase usage.
4. Special case — Pydantic: verify `model_fields` (v2) not `.fields` (v1) in any touched code.
5. Special case — ruff: do NOT upgrade without creating an ADR (ruff version is exactly pinned at 0.8.6).
6. If CI dev deps affected: update `requirements-ci.txt` manually.
7. Run `make agent-check` — all 7 gates must pass.
8. PR description must include: old version, new version, changelog highlights, breaking change assessment.

---

## Import Restrictions by Module

> For binding rule definition, see AGENT.md C-01.
> This section is informational context only.

| Module | May Import | May NOT Import |
|---|---|---|
| `engine/` | pydantic, structlog, redis, neo4j, httpx, pyyaml | fastapi |
| `app/engines/` (excl. handlers.py) | `engine.*`, `app.models.*`, pydantic, structlog | fastapi |
| `app/engines/handlers.py` | fastapi, `engine.*`, `app.models.*` | (unrestricted) |
| `app/api/` | fastapi, `app.engines.handlers`, `app.models.*` | `engine.*` directly |
| `app/models/` | pydantic, typing | fastapi, `engine.*`, structlog |

---

## License Summary

| License | Packages | Compliance Note |
|---|---|---|
| MIT | fastapi, pydantic, httpx, uvicorn, structlog, ruff | Permissive — OK |
| Apache 2.0 | pyyaml, sqlalchemy, alembic, redis, neo4j | Permissive — OK |
| BSD | pytest, mypy | Permissive — OK |

No GPL-licensed dependencies. Compliance agents: verify this table remains accurate on any dep addition.
