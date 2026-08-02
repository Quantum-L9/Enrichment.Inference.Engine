# AGENTS.md — Enrichment.Inference.Engine

## What This Repo Is

Universal domain-aware entity enrichment API.
Stack: Python 3.12 / FastAPI app surface / SDK transport runtime / Perplexity LLM / Neo4j graph / Redis cache / PostgreSQL state.

---

## Three Rules Every Agent Must Know

1. **Run `make agent-check` before every commit** — all 8 gates must pass.
2. **Respect your Tier** — see Autonomy Tiers below. T4/T5 changes require human review.
3. **This file is law** — if you see a conflict between documents, AGENTS.md wins.

---

## Agent Operating Model

This document defines the rules of engagement for AI agents (Manus, Copilot, Cursor, etc.) operating on this repository.

### Autonomy Tiers

| Tier | Scope | Allowed Without Human Review |
|------|-------|------------------------------|
| **T0 — Read-Only** | Browse, index, analyze, report | Always |
| **T1 — Lint & Fix** | Auto-fix lint, formatting, import ordering | Always |
| **T2 — Test & CI** | Add/modify tests, CI config, pre-commit | PR required |
| **T3 — Engine Logic** | Modify `app/engines/`, `app/score/`, `app/health/`, `app/services/` | PR + review required |
| **T4 — Transport Contract** | Modify SDK runtime transport/wiring bundle | PR + 2 reviewers |
| **T5 — Schema** | Modify `app/models/`, `kb/` rule files | PR + 2 reviewers + CI green |

### Protected Files — Never Modify Without Human Review (T4/T5)

| File | Tier | Why |
|------|------|-----|
| `app/main.py` | T4 | SDK runtime bootstrap and transport ingress ownership |
| `app/api/v1/chassis_endpoint.py` | T4 | Transport-adjacent HTTP routes |
| `app/services/chassis_handlers.py` | T4 | Supplemental SDK handler registration |
| `app/engines/orchestration_layer.py` | T4 | Canonical SDK handler registration + cross-node orchestration wiring |
| `app/engines/handlers.py` | T4 | Engine action handlers — must stay aligned with runtime registrations |
| `app/engines/graph_sync_client.py` | T4 | Gate transport contract |
| `app/models/` (all files) | T5 | Pydantic schema — downstream breaking change risk (`app/models/loop_schemas.py`, `app/models/field_confidence.py`) |
| `app/score/score_models.py` | T5 | Score schema contract |
| `app/health/health_models.py` | T5 | Health schema contract |
| `kb/` rule files | T5 | Knowledge base — requires review + CI green |
| `.github/workflows/` | T2 | CI pipeline integrity |
| `AGENTS.md`, `CLAUDE.md`, `GUARDRAILS.md` | T4 | Governance documents |
| `Dockerfile`, `docker-compose*.yml` | T3 | Infrastructure immutability |

### Deprecated Compatibility Artifacts

These files are **not** part of the active production transport path and must not be used as the constitutional source of transport behavior:

- `chassis/envelope.py`
- `chassis/router.py`
- `chassis/registry.py`

They may remain in the repo temporarily for compatibility, tests, migration safety, or historical reference. Do not reintroduce them into production dispatch.

### First-Order Thinking Gates

Before any modification, run the 5 Gates:

1. **GATE 1 — Functional before cosmetic.** Does this change make the engine work better, or just look better?
2. **GATE 2 — Scope check.** Is this change within the PR's stated scope?
3. **GATE 3 — Contract preservation.** Does this change break any runtime/graph/score contract?
4. **GATE 4 — Test coverage.** Does this change have corresponding test coverage?
5. **GATE 5 — Rollback safety.** Can this change be reverted without data loss?

### Directory Ownership

| Directory / File | Owner | Agent Tier |
|-----------|-------|------------|
| `app/engines/` | Engine team | T3+ |
| `app/api/v1/chassis_endpoint.py` | Platform team | T4+ |
| `app/services/chassis_handlers.py` | Platform team | T4+ |
| `app/engines/orchestration_layer.py` | Platform team | T4+ |
| `app/engines/handlers.py` | Platform team | T4+ |
| `app/engines/graph_sync_client.py` | Platform team | T4+ |
| `app/main.py` | Platform team | T4+ |
| `app/models/` | Schema team | T5 |
| `app/score/` | Score team | T3+ |
| `app/health/` | Health team | T3+ |
| `app/api/` | API team | T3+ |
| `app/services/` | Services team | T3+ |
| `kb/` | Knowledge team | T5 |
| `tests/` | Any | T2+ |
| `tools/` | Platform team | T2+ |
| `.github/` | Platform team | T2+ |

---

## Feature Flag & Config State

Current off-by-default flag posture after the full-throttle activation change. Agents
changing these must keep this table and `CHANGELOG.md` in sync.

**Activated (`true`) — descriptive/config, test-proven, no live external call turned on:**

| Flag | File | Real effect |
|------|------|-------------|
| `linkedin.enabled` | `config/enrichment_sources.yaml` | Catalog intent only — file is not imported by any module |
| `enabled` (×4) | `docs/contracts/dependencies/{apollo,clearbit,hunter,zoominfo}.yaml` | Dependency-contract attestation; runtime call still gated by `*_API_KEY` + `config/provider_config.yaml` |
| `ingress`, `autoscaling`, `pdb` `.enabled` | `infra/k8s/helm/enrichment-api/values-dev.yaml` | Dev Helm overlay only |
| `auth_enabled` | `monitoring/loki/loki-config.yaml` | Loki multi-tenant mode — requires `X-Scope-OrgID` from Promtail/Grafana |
| `analytics.reporting_enabled` | `monitoring/loki/loki-config.yaml` | Anonymous usage telemetry to Grafana Labs |

**Held OFF by design — do not flip without explicit human sign-off:**

| Flag | File | Why held |
|------|------|----------|
| `GF_USERS_ALLOW_SIGN_UP` | `docker-compose.yml` | Enabling Grafana self-signup loosens an access control on the observability stack (which also ships a default admin password). Stays `false`. |

`auto_pause_after_reviewed_commits` was removed from `.coderabbit.yaml` (never auto-pause review).

The real enrichment runtime gate is `config/provider_config.yaml` + the provider's API key
env var — **not** the dependency-contract or catalog `enabled` fields above.

---

## Mandatory Pre-Commit Command

```bash
make agent-check
```

All 8 gates must pass before any commit.

| Gate           | Command                               | Blocks On                                 |
| -------------- | ------------------------------------- | ----------------------------------------- |
| 1/7 LINT       | ruff check .                          | Any lint error                            |
| 2/7 FORMAT     | ruff format --check .                 | Formatting inconsistency                  |
| 3/7 TYPES      | mypy app                              | Type errors (non-blocking per WAIVER-001) |
| 4/7 UNIT TESTS | pytest tests/unit/ tests/compliance/  | Test failure or coverage < 60%            |
| 5/7 CI TESTS   | pytest tests/ci/                      | Contract call enforcement failure         |
| 6/7 AUDIT      | python tools/audit_engine.py --strict | CRITICAL/HIGH rule violations             |
| 7/7 CONTRACTS  | python tools/verify_contracts.py      | Contract manifest integrity failure       |

Quick fix before re-check: `make agent-fix` (auto-fixes ruff lint + format)

---

## CI Requirements

All PRs must pass:

* `make lint` — Ruff + MyPy
* `make test` — pytest with coverage
* `make audit` — 27-rule audit engine
* `make verify` — Contract verification
* Compliance tests (banned patterns, field names, import resolution, architecture)
* Contract call enforcement (repository_contract_pairs.yaml)

---

## Architectural Contracts

| ID | Contract | Severity |
|----|----------|----------|
| C-01 | `from fastapi import` only in app/api/, app/main.py, and transport-adjacent modules explicitly intended for HTTP routes | CRITICAL |
| C-02 | Handler contract: `async def handle_*(tenant, payload) -> dict` is the primary production signature for SDK-registered handlers | CRITICAL |
| C-03 | Tenant isolation in all Neo4j queries: `WHERE n.tenant_id = $tenant` | CRITICAL |
| C-04 | structlog only — `print()` forbidden in engine code | HIGH |
| C-05 | `list[T]`, `T \| None` — never `List[]`, `Optional[]`, `Dict[]` | HIGH |
| C-06 | `eval()`, `exec()`, `compile()`, `pickle` — forbidden always | CRITICAL |
| C-07 | Cypher strings via `sanitize_label()` — no f-string injection | CRITICAL |
| C-08 | `yaml.safe_load()` only — never `yaml.load()` | CRITICAL |
| C-09 | All env vars must use `L9_` prefix except approved infrastructure-standard names | HIGH |
| C-10 | Zero hardcoded credentials — env vars via pydantic-settings only | CRITICAL |
| C-11 | `TransportPacket` and SDK transport objects are immutable at boundaries — never mutate in place. Deprecated local chassis dict envelopes are not part of production dispatch. | CRITICAL |
| C-12 | No `Field(alias=...)` in Pydantic models | HIGH |
| C-13 | Transport contract lockstep: `app/main.py`, `app/api/v1/chassis_endpoint.py`, `app/services/chassis_handlers.py`, `app/engines/orchestration_layer.py`, `app/engines/handlers.py`, and `app/engines/graph_sync_client.py` must stay aligned | CRITICAL |
| C-14 | New SDK action registration requires corresponding handler + test in same PR | HIGH |
| C-15 | Coverage >= 60% — never lower the threshold | HIGH |
| C-16 | Python 3.12+ — no backports, no 3.11-only APIs | CRITICAL |
| C-17 | No camelCase Python field names | HIGH |
| C-18 | Ruff ignore list is frozen — do not add/remove ignores | HIGH |
| C-19 | GDS jobs must be spec-driven from domain YAML gds_jobs section | MEDIUM |
| C-20 | L9_META header required in all template-managed files | HIGH |
| C-21 | `/v1/execute` is owned by the SDK transport runtime. Do not route production ingress through `chassis/router.py` or `chassis/registry.py`. | CRITICAL |

---

## Canonical Import Patterns

```python
# CORRECT — module-level imports, Pydantic v2 runtime safe
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import structlog
from pydantic import BaseModel, Field, model_validator

from constellation_node_sdk.transport import TransportPacket, create_transport_packet
from engine.utils.security import sanitize_label
from engine.utils.neo4j import execute_cypher

# WRONG — never do these
from typing import Optional, List, Dict      # C-05: use T | None, list[T], dict[K,V]
import yaml; yaml.load(...)                  # C-08: always yaml.safe_load()
print("debug")                               # C-04: always structlog
```

---

## Forbidden Patterns

| Rule ID  | Pattern                                                                  | Severity |
| -------- | ------------------------------------------------------------------------ | -------- |
| SEC-001  | f-string Cypher without sanitize_label()                                 | CRITICAL |
| SEC-002  | `eval(` anywhere                                                         | CRITICAL |
| SEC-003  | `exec(` anywhere                                                         | CRITICAL |
| SEC-004  | `pickle.loads(`                                                          | CRITICAL |
| SEC-005  | `subprocess.shell=True`                                                  | CRITICAL |
| SEC-006  | `compile(` in engine code                                                | CRITICAL |
| SEC-007  | `yaml.load(` not safe_load                                               | CRITICAL |
| ARCH-001 | `from fastapi import` outside allowed modules                            | CRITICAL |
| ARCH-002 | `import fastapi` outside allowed modules                                 | CRITICAL |
| ARCH-003 | transport dispatch via deprecated `chassis/router.py` in production code | CRITICAL |
| ERR-001  | `except:` bare (no exception type)                                       | HIGH     |
| OBS-001  | `structlog.configure(` in engine/                                        | HIGH     |
| STUB-001 | `pass` in a non-test function body                                       | HIGH     |
| STUB-002 | `...` as a non-test function body                                        | HIGH     |
| STUB-003 | TODO / FIXME in committed code                                           | MEDIUM   |
| TYPE-001 | `Optional[` anywhere                                                     | HIGH     |
| TYPE-002 | `List[` anywhere                                                         | HIGH     |
| TYPE-003 | `Dict[` anywhere                                                         | HIGH     |

---

## Prohibited Actions

* **Never** reintroduce production dispatch through `chassis/envelope.py`, `chassis/router.py`, or `chassis/registry.py`
* **Never** change SDK transport/runtime ingress semantics without lockstep updates to the active transport bundle
* **Never** add new SDK action registrations without corresponding handler + test
* **Never** introduce FastAPI imports in engine modules outside explicitly allowed transport-adjacent files
* **Never** use `eval()`, `exec()`, `compile()`, `pickle`
* **Never** hardcode API keys, tokens, or secrets
* **Never** use `print()` in engine code (use `structlog`)
* **Never** add `Field(alias=...)` to Pydantic models
* **Never** use camelCase for Python field names
* **Never** modify authentication or authorization logic without review
* **Never** modify `.github/workflows/` without review
* **Never** log API keys, partner data, or LLM responses at INFO level or above

---

## Agent Modes

### Code Generation

1. Determine your Tier for the files being modified.
2. Check forbidden patterns table before writing.
3. Use canonical import patterns.
4. Run `make agent-check` before committing.
5. Every new public function needs a docstring and type annotations.

### Code Review (PR)

1. Load `AI_AGENT_REVIEW_CHECKLIST.md` as primary guide.
2. Check all C-01 through C-21 contracts against changed files.
3. Comment format: `CONTRACT C-{N} VIOLATION — {rule}`.

### Refactoring

1. Confirm the change is within your Tier scope.
2. Verify all changed function signatures still match C-02.
3. Run `make test-compliance` and `make test-contracts` after refactoring.
4. If changing transport/runtime files, verify SDK ingress ownership (C-21) is preserved.

### Question-Answering and Analysis

* T0 operation — always permitted.
* Reference specific section titles and contract IDs in answers.

---

## Agent Capabilities

### Permitted Commands

* `make agent-check` — full gate sequence
* `make agent-fix` — auto-fix lint/format
* `make test`, `make test-unit`, `make test-compliance`, `make test-contracts`
* `make audit`, `make audit-json` — informational audit
* `make verify` — contract verification
* `python tools/audit_engine.py` — direct audit invocation
* `python tools/verify_contracts.py` — direct verification

### Forbidden Commands

* `make prod`, `make prod-build`, `make deploy` — production infrastructure
* `make dev-clean` — destroys volumes
* `docker system prune` — destructive
* `git push --force` or `git reset --hard`
* `alembic upgrade head` in production context

---

## Quick Reference

| Question                            | Section                                |
| ----------------------------------- | -------------------------------------- |
| What can I do without human review? | Autonomy Tiers                         |
| What is forbidden?                  | Prohibited Actions, Forbidden Patterns |
| Which files can I never modify?     | Protected Files (T4/T5)                |
| What are the 8 gates?               | Mandatory Pre-Commit Command           |
| What are the contracts?             | Architectural Contracts (C-01 to C-21) |
| What owns `/v1/execute`?            | C-21 + Protected Files                 |

---

## Cursor Cloud specific instructions

Durable, non-obvious notes for running this service in the Cursor Cloud VM. The startup update script already provisions a `.venv` and installs `pip install -e ".[dev]"`, so do not repeat dependency installation here.

### Runtime facts
- Python 3.12 only (`.python-version` = 3.12). The dev virtualenv lives at `.venv/`; run tools as `.venv/bin/<tool>` (the `Makefile` auto-selects `.venv/bin/python` when present).
- `constellation-node-sdk` is a pinned **git** dependency from the private `Quantum-L9/Gate_SDK` repo. Installs rely on authenticated git (the cloud VM's `gh`/git credentials). If `pip install` fails on that line, it is a git-auth/network issue, not a code issue.
- The system package `python3.12-venv` is required to create the venv and is baked into the VM snapshot. If `python3 -m venv` ever fails on a fresh pod, run `sudo apt-get update && sudo apt-get install -y python3.12-venv`.

### Running the app (dev)
- No Docker is needed to run the API. Start it directly: `.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000` (add `--reload` for hot reload). `make dev` uses Docker Compose and is not required in the VM.
- The ASGI object is `app.main:app` (built via the SDK's `create_node_app`).
- Startup **degrades gracefully**: Redis and Postgres are lazy/optional (the app logs `redis_connected` even with no Redis running because the client connects lazily), and no external API keys are required to boot. `GET /api/v1/health` is unauthenticated and returns `{"status":"ok","version":"2.3.0",...}`.

### Auth + enrichment testing
- `/api/v1/enrich` and `/api/v1/enrich/batch` require an `X-API-Key` header whose SHA-256 hex digest matches `API_KEY_HASH`. Create a gitignored `.env.local` (loaded via `.env`/`.env.local`) with a throwaway local key — generate the pair yourself and never commit either value:

  ```bash
  python3 - <<'PY'
  import hashlib, pathlib, secrets
  key = secrets.token_urlsafe(24)
  digest = hashlib.sha256(key.encode()).hexdigest()
  pathlib.Path(".env.local").write_text(f"API_KEY_HASH={digest}\n", encoding="utf-8")
  print("Wrote .env.local with API_KEY_HASH")
  print(f"X-API-Key (save once, do not commit): {key}")
  PY
  ```

  For Kubernetes secret generation, prefer `infra/k8s/scripts/generate-secrets.sh`. `.env.local` is gitignored and persists in the VM snapshot; recreate it if missing.
- A live enrich call returns HTTP 200 with a structured `EnrichResponse`, but `state` will be `"failed"` (`no_valid_responses ... APIConnectionError`) unless a valid `PERPLEXITY_API_KEY` is set — the LLM call is the only piece that needs an external secret. The successful-enrichment path (mocked LLM) is covered by the unit tests.

### Lint / test gotchas
- Lint is `ruff` only in practice; `mypy` is non-blocking per `WAIVER-001`. `ruff format --check .` currently flags two pre-existing files on `main` (`app/agents/deal_risk.py`, `app/score/score_models.py`) — not introduced by env setup; do not "fix" `score_models.py` (T5-protected).
- Do not run the whole suite blindly: `tests/services/test_outcome_delegator.py` fails at **collection** on `main` (references `OutcomeVerdict.GRAPH_REJECTED`, which does not exist). The canonical gate suites (`tests/unit`, `tests/compliance`, `tests/ci`) pass — run those.
- `pytest.ini` hard-codes `--cov-fail-under=60`, computed against all of `app`. Running a narrow subset will report low coverage and exit non-zero even when every test passes; that is a coverage-gate artifact, not a test failure. Running tests regenerates the gitignored `coverage.xml`/`htmlcov` — discard those.
