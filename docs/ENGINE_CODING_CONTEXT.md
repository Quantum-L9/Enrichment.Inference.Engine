# Engine coding context (crib sheet)

> **Non-authoritative.** SSOT for contracts is `.cursorrules` and [`AGENT.md`](../AGENT.md). This file preserves the prior root `CLAUDE.md` engine-focused summary before governance [`CLAUDE.md`](../CLAUDE.md) moved to repo root.

---

# AI Coding Context — L9 Golden Repo / L9 Constellation Engine

## What This Is
An L9 constellation engine. FastAPI chassis + domain engine. Single ingress (`POST /v1/execute`).
Read `L9_Platform_Architecture.md` before writing any code.

## The One Rule
The chassis already handles: auth, rate limiting, tenant resolution, logging, metrics, routing, Docker, CI/CD.
**You build the engine. Nothing else.**

## Architecture
```
POST /v1/execute → L9 Chassis → Action Router → engine/handlers.py → domain logic
```
- Engine registers handlers: `chassis.router.register_handler("match", handle_match)`
- Handler signature: `async def handle_{action}(tenant: str, payload: dict) -> dict`
- Engine NEVER imports FastAPI, never creates routes, never touches auth/logging/metrics config

## 20 Contracts (enforced by contract_scanner.py + CI)

### Layer 1 — Chassis Boundary (1–5)
1. Single ingress — `POST /v1/execute` only. No custom routes.
2. Handler interface — `async def handle_<action>(tenant: str, payload: dict) -> dict`. Only `engine/handlers.py` imports chassis.
3. Tenant isolation — chassis resolves tenant. Engine receives it as string. Every Neo4j query scopes to tenant database.
4. Observability inherited — engine uses `structlog.get_logger(__name__)` only. Never configures logging.
5. Infrastructure is template — no Dockerfile, docker-compose, CI, or Terraform in engine/.

### Layer 2 — Packet Protocol (6–8)
6. PacketEnvelope is the only data container between services.
7. Immutability + content_hash — `frozen=True`. Mutations via `.derive()`. `content_hash` is UNIQUE constraint.
8. Lineage + audit — all derived packets set `parent_id`, `root_id`, increment `generation`.

### Layer 3 — Security (9–11)
9. Cypher injection prevention — labels/types via `sanitize_label()` only. Values always parameterized.
10. Prohibited factors — compliance fields blocked at compile time. Never runtime.
11. PII handling — declared in domain spec. Never logged. Chassis filters set it.

### Layer 4 — Engine Architecture (12–16)
12. Domain spec is SSOT — all behavior from `spec.yaml`. DomainPackLoader → DomainConfig.
13. Gate-then-score — gates compile to Cypher WHERE, scoring to Cypher WITH. Zero Python post-filtering.
14. NULL semantics are deterministic — per-gate `null_behavior: pass | fail`. Compiler handles it.
15. Bidirectional matching — invertible gates swap props on direction reversal. Compiler handles it.
16. File structure is fixed — see directory layout below.

### Layer 5 — Testing + Quality (17–18)
17. Tests: unit (gate compilation, scoring math), integration (testcontainers-neo4j, no mock drivers), compliance, performance (<200ms p95).
18. L9_META on every file.

### Layer 6 — Graph Intelligence (19–20)
19. GDS jobs are declarative — declared in `spec.gds_jobs`, not hardcoded.
20. KGE embeddings — CompoundE3D 256-dim, domain-specific, never cross-tenant.

## Protected Files — Explicit Approval Required
- `engine/handlers.py` — chassis bridge
- `docker-compose.prod.yml` — production infra
- `.github/workflows/ci-quality.yml` — CI gate

## Canonical Directory Layout
```
engine/handlers.py      ← ONLY chassis bridge
engine/config/          ← domain spec schema + loader
engine/gates/           ← gate compiler + types (10 types)
engine/scoring/         ← scoring assembler (9 computation types)
engine/traversal/       ← traversal assembler
engine/sync/            ← sync generator
engine/gds/             ← GDS scheduler (APScheduler)
engine/graph/           ← Neo4j async driver wrapper
engine/compliance/      ← prohibited factors + PII + audit
engine/packet/          ← PacketEnvelope bridge
chassis/                ← thin chassis adapter
domains/                ← {domain_id}/spec.yaml per vertical
```

## Banned Patterns (CRITICAL — merge blocked)
- `f-string Cypher MATCH` without `sanitize_label()` → SEC-001
- `eval()`, `exec()`, `pickle.load()` → SEC-002/003/006
- `from fastapi import` in engine/ → ARCH-001
- `INSERT INTO packet_store` in engine/ → MEM-001
- `raise NotImplementedError` outside tests/ → STUB-001
- `# TODO` or `# PLACEHOLDER` comments → STUB-002/003
- Uppercase `packet_type` values → PKT-001
- `yaml.load()` without SafeLoader → SEC-007

## Code Style
- Python 3.12+, async/await for all I/O
- Pydantic v2 BaseModel (frozen where appropriate)
- ruff format (88-char), mypy --strict engine/
- structlog: include `tenant`, `trace_id`, `action` in every log context
- snake_case everywhere. No `Field(alias=...)`.

## Ruff Patterns to Avoid
```python
# ❌ raise ValueError(f"invalid {x}")
# ✅ msg = f"invalid {x}"; raise ValueError(msg)  # prevents EM101/102

# ❌ def f(x: str = None)
# ✅ def f(x: str | None = None)  # prevents RUF013

# ❌ datetime.now()
# ✅ datetime.now(tz=UTC)  # prevents DTZ005
```

## What NOT to Build
- FastAPI routes, APIRouter → chassis (contract 1)
- Auth, tenant resolution, CORS middleware → chassis (contract 3)
- Logging/structlog configuration → chassis (contract 4)
- Prometheus setup → chassis (contract 4)
- Dockerfile, docker-compose, CI/CD → l9-template (contract 5)
- Custom request/response HTTP schemas → universal envelope (contract 1)
- String-interpolated Cypher without sanitize_label → security (contract 9)
- Python post-filtering of match results → gate-then-score in Cypher (contract 13)
- Hardcoded GDS scheduling → declarative spec (contract 19)
- `eval()`, `exec()`, `pickle.load()`, `yaml.load()` without SafeLoader → banned (contract 9)
