# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: enrichment
# layer: [governance]
# tags: [L9_TEMPLATE, troubleshooting, errors, known-issues]
# owner: platform
# status: active
# token_estimate: 680
# ssot_for: [error-diagnosis, startup-failures, common-mistakes]
# load_when: [error, startup_failure, unexpected_behavior]
# references: [EXECUTION_FLOWS.md, CONFIG_ENV_CONTRACT.md, CI_WHITELIST_REGISTER.md]
# --- /L9_META ---

# TROUBLESHOOTING.md — Error Diagnosis & Common Mistakes

**VERSION**: 1.0.0 | **SHA_BASELINE**: 358d15d | **LAST_REVIEWED**: 2026-04-01

---

## Startup Failures

### pydantic ValidationError on startup
**Symptom**: `ValidationError: N validation errors for L9Settings — Field required`
**Cause**: One or more required environment variables are missing.
**Fix**: Copy `.env.example` to `.env` and fill in missing values. See CONFIG_ENV_CONTRACT.md §Minimum .env for `make dev`.

### Neo4j connection refused
**Symptom**: `ServiceUnavailable: Failed to establish connection to bolt://localhost:7687`
**Cause**: Neo4j is not running.
**Fix**: `docker compose up neo4j -d` or verify `L9_NEO4J_URI` in `.env`.

### Redis connection refused
**Symptom**: `ConnectionRefusedError: [Errno 111] Connect refused — redis://localhost:6379`
**Cause**: Redis is not running.
**Fix**: `docker compose up redis -d` or verify `REDIS_URL` in `.env`.

### Port already in use
**Symptom**: `OSError: [Errno 98] Address already in use`
**Cause**: Another process is using `L9_API_PORT` (default 8000).
**Fix**: `lsof -i :8000 | kill -9` or change `L9_API_PORT` in `.env`.

---

## CI Gate Failures

See CI_WHITELIST_REGISTER.md §Agent Response to CI Failures for the full decision matrix.

### make agent-check fails on gate 5 (AUDIT)
**Symptom**: `audit_engine.py: CRITICAL violation SEC-001 — f-string Cypher`
**Fix**: Replace f-string Cypher with `sanitize_label()` calls. See AGENT.md §C-07.

### make agent-check fails on gate 7 (CONTRACTS)
**Symptom**: `verify_contracts.py: L9_META header missing in app/engines/new_module.py`
**Fix**: Add the L9_META header block to the top of the file. Copy from any existing file in `app/`.

### Compliance chassis isolation fails
**Symptom**: `from fastapi import ... found in engine/utils/my_module.py`
**Fix**: Remove FastAPI import. Engine modules are chassis-agnostic. See AGENT.md §C-01.

---

## Common Agent Mistakes

| Mistake | Symptom | Fix |
|---|---|---|
| Using `Optional[X]` | compliance-terminology CI failure | Use `X | None` |
| Using `List[X]` | compliance-terminology CI failure | Use `list[X]` |
| Using `print()` in engine code | compliance-terminology CI failure | Use `structlog.get_logger().info(...)` |
| Mutating `TransportPacket` / frozen models | `ValidationError` or SDK guard failure | Never mutate — create a new packet / derived dict |
| Missing test marker | pytest collection warning | Add `@pytest.mark.unit` or `@pytest.mark.integration` |
| Yaml.load() without SafeLoader | SEC-007 audit failure | Use `yaml.safe_load()` |
| Adding ruff ignore | INV-17 violation | Do NOT add ruff ignores — frozen list |
| Modifying chassis_contract.py alone | T4 gate — 2 reviewers required | Always update handlers.py in same PR |

---

## Test Isolation Issues

**Symptom**: Integration tests pass locally but fail in CI.
**Cause**: Test depends on external service (Neo4j, Redis, Perplexity) not mocked in CI.
**Fix**: Use `@pytest.mark.integration` + ensure test is skipped when `TESTING=True`.

**Symptom**: Tests leak state between runs.
**Cause**: Shared mutable fixture or missing cleanup.
**Fix**: Use `yield` fixtures with cleanup, or `@pytest.fixture(autouse=True)` for reset.

---

## Known Unknowns (Do Not File Bugs For These)

- Neo4j GDS procedure execution details: undocumented — see EXECUTION_FLOWS.md §Known Documentation Gaps.
- PostgreSQL access layer internals: undocumented — see EXECUTION_FLOWS.md §Known Documentation Gaps.
- AWS Secrets Manager integration: aspirational only — see CONFIG_ENV_CONTRACT.md §AWS Secrets Manager.
