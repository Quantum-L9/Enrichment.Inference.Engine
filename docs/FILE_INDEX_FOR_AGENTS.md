# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: enrichment
# layer: [governance]
# tags: [L9_TEMPLATE, agent, file-index, lookup]
# owner: platform
# status: active
# token_estimate: 1500
# ssot_for: [file-lookup, file-modification-rules, token-budgets]
# load_when: [new_file_creation, file_lookup, onboarding]
# references: [AGENTS.md, REPO_MAP.md, ARCHITECTURE.md, EXECUTION_FLOWS.md]
# --- /L9_META ---

# FILE_INDEX_FOR_AGENTS.md — File Lookup Index

**VERSION**: 2.2.0 | **SHA_BASELINE**: 358d15d | **LAST_REVIEWED**: 2026-04-11

---

## Bootstrap Reading Order (New Agent)

| Step | File | Tokens | Purpose |
|---|---|---:|---|
| 1 | [AGENTS.md](AGENTS.md) | ~2,800 | Contracts, tiers, active transport bundle, forbidden patterns |
| 2 | [GUARDRAILS.md](GUARDRAILS.md) | ~900 | Hard prohibitions and safety constraints |
| 3 | [ARCHITECTURE.md](ARCHITECTURE.md) | ~1,200 | Live SDK runtime topology |
| 4 | [REPO_MAP.md](REPO_MAP.md) | ~1,400 | Directory ownership and module boundaries |
| 5 | [EXECUTION_FLOWS.md](EXECUTION_FLOWS.md) | ~1,000 | Initialization and runtime execution paths |
| Total | | ~7,300 | First-pass budget |

---

## Governance Pack — Token Budget

| File | Est. Tokens | Load Priority |
|---|---:|---|
| AGENTS.md | ~2,800 | ALWAYS |
| CLAUDE.md | ~700 | Claude-only addendum |
| INVARIANTS.md | ~2,600 | On-demand |
| REPO_MAP.md | ~1,400 | Trigger: new file / ownership question |
| EXECUTION_FLOWS.md | ~1,000 | Trigger: runtime/control-flow change |
| ARCHITECTURE.md | ~1,200 | Trigger: topology / transport question |
| CONFIG_ENV_CONTRACT.md | ~1,200 | Trigger: env vars |
| docs/contracts/config/env-contract.yaml | variable | Machine SSOT for env vars |
| CI_WHITELIST_REGISTER.md | ~1,200 | Trigger: CI gate / waiver question |
| AI_AGENT_REVIEW_CHECKLIST.md | ~1,600 | PR review |
| GUARDRAILS.md | ~900 | ALWAYS |
| FILE_INDEX_FOR_AGENTS.md | ~1,500 | ALWAYS |
| Pack Total | ~16,000+ | — |

---

## File Directory (Structured Lookup)

| Need | File | Section |
|---|---|---|
| What owns `/v1/execute`? | AGENTS.md | C-21, Protected Files |
| What is the active transport bundle? | AGENTS.md | C-13 |
| What is deprecated? | AGENTS.md | Deprecated Compatibility Artifacts |
| What is the live runtime topology? | ARCHITECTURE.md | SDK Transport Runtime |
| What are the execution paths? | EXECUTION_FLOWS.md | Transport Execution Flow |
| Which files are protected? | AGENTS.md | Protected Files |
| What are the invariants? | INVARIANTS.md | INV-1 through INV-20 |
| What are the PR review checks? | AI_AGENT_REVIEW_CHECKLIST.md | Phase 1-5 |
| What CI gates block merge? | CI_WHITELIST_REGISTER.md | Merge-Blocking Gates |
| What are the directory boundaries? | REPO_MAP.md | Module Boundary |
| Which files are deprecated compatibility artifacts? | REPO_MAP.md | Deprecated Files |

---

## File Modification Rules

### NEVER Modify Without T4/T5 Human Review

Per [AGENTS.md](AGENTS.md), verify the live table there if this list drifts:

- `app/main.py`
- `app/api/v1/chassis_endpoint.py`
- `app/services/chassis_handlers.py`
- `app/engines/orchestration_layer.py`
- `app/engines/handlers.py`
- `app/engines/graph_sync_client.py`
- `app/models/` (any file)
- `kb/` (any rule file)
- `.github/workflows/` (any workflow)
- `GUARDRAILS.md`, `AGENTS.md`, `CLAUDE.md`
- `Dockerfile`, `docker-compose.yml`, `docker-compose.prod.yml`

### Deprecated Compatibility Artifacts

These files are **not** the constitutional source of production transport behavior:

- `chassis/envelope.py`
- `chassis/router.py`
- `chassis/registry.py`

They may remain temporarily for compatibility, migration safety, or historical reference. Do not route production ingress or handler dispatch through them.

### ALWAYS Create With L9_META Header

Files managed by L9 templates must include the L9_META header block.

### Safe to Modify at T1/T2

- `tests/` (all test files)
- `docs/` (documentation)
- `tools/audit_engine.py`, `tools/verify_contracts.py` (with PR)
- `README.md`, `CHANGELOG.md`
- `.env.example` (never `.env` itself)

---

## Generated / Artifact Files (Do Not Edit Directly)

| File | Generator | Purpose |
|---|---|---|
| coverage.xml | pytest --cov-report=xml | CI coverage report |
| htmlcov/ | pytest --cov-report=html | Human-readable coverage |
| sbom.spdx.json | supply-chain jobs | SBOM output |
| scorecard.sarif | supply-chain jobs | OpenSSF scorecard |
| reports/ | Various tools | Audit and scan reports |

---

## Undiscovered Files Protocol

If you encounter a file not listed in this index:
1. Check for an L9_META header.
2. Check REPO_MAP.md for directory ownership.
3. Apply the most restrictive tier for the containing directory until confirmed otherwise.
4. If the file affects the SDK runtime transport bundle, treat it as T4 until proven otherwise.
5. File a docs-gap issue if it should be added here.
