# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: enrichment
# layer: [governance]
# tags: [L9_TEMPLATE, repo-map, structure, ownership]
# owner: platform
# status: active
# token_estimate: 1217
# ssot_for: [directory-structure, module-boundaries, codeowners-map]
# load_when: [new_file, directory_question, ownership_question]
# references: [AGENTS.md, .github/CODEOWNERS, REPO_MAP.md]
# --- /L9_META ---

# REPO_MAP.md — Repository Structure Map

**VERSION**: 2.1.0 | **SHA_BASELINE**: 358d15d | **LAST_REVIEWED**: 2026-04-11

> SSOT for directory structure and module ownership.
> Module boundary rules (transport/API vs engine separation): see AGENTS.md C-01/C-21.
> Execution flows for entry points: see EXECUTION_FLOWS.md.

---

## Module Boundary (SSOT — All Other Docs Reference Here)

API / TRANSPORT SURFACE:
- `app/main.py`
- `app/api/`
- SDK runtime registration surface
- Production transport ingress is SDK-owned `/v1/execute`

May import:
- `fastapi`
- `constellation_node_sdk`
- `app.models.*`
- engine/service modules needed for delegation

ENGINE (Business logic — transport-agnostic):
- `engine/`
- `app/engines/` (except transport-adjacent registration/wiring files)
- `app/score/`
- `app/health/`
- `app/services/`

MUST NOT import:
- `fastapi` in pure engine modules

MODELS (Schema — frozen Pydantic v2):
- `app/models/`
- Imported by both runtime/API and engine layers

DEPRECATED COMPATIBILITY ARTIFACTS:
- `chassis/envelope.py`
- `chassis/router.py`
- `chassis/registry.py`

These are not part of the active production dispatch path.

---

## Directory Map

| Directory / File | Purpose | Owner | Agent Tier | CODEOWNERS |
|---|---|---|---|---|
| app/ | FastAPI application root + SDK runtime assembly | Platform | T2+ | @cryptoxdog |
| app/main.py | SDK transport ingress bootstrap + mounted app surface | Platform | T4 | @cryptoxdog |
| app/api/ | HTTP route handlers | API team | T3+ | @cryptoxdog |
| app/api/v1/chassis_endpoint.py | Transport-adjacent app routes (not `/v1/execute`) | Platform | T4 | @cryptoxdog |
| app/engines/ | Core enrichment engine modules | Engine team | T3+ | @cryptoxdog |
| app/engines/handlers.py | Engine action handlers | Platform | T4 | @cryptoxdog |
| app/engines/orchestration_layer.py | Canonical SDK handler registration + orchestration wiring | Platform | T4 | @cryptoxdog |
| app/engines/graph_sync_client.py | Gate transport client | Platform | T4 | @cryptoxdog |
| app/models/ | Pydantic v2 schemas (frozen) | Schema team | T5 | @cryptoxdog |
| app/score/ | Scoring engine | Score team | T3+ | @cryptoxdog |
| app/health/ | Health check logic | Health team | T3+ | @cryptoxdog |
| app/services/ | External service clients and support services | Services team | T3+ | @cryptoxdog |
| app/services/chassis_handlers.py | Supplemental SDK inbound handler registrations | Platform | T4 | @cryptoxdog |
| engine/ | Chassis-agnostic engine utilities | Engine team | T3+ | @cryptoxdog |
| engine/utils/security.py | sanitize_label(), auth utils | Platform | T4 | @cryptoxdog |
| kb/ | Knowledge base YAML rule files | Knowledge team | T5 | @cryptoxdog |
| tests/ | All test suites | Any | T2+ | — |
| tests/unit/ | Unit tests (no external deps) | Any | T2+ | — |
| tests/integration/ | Integration tests | Any | T2+ | — |
| tests/compliance/ | Architecture compliance tests | Platform | T2+ | @cryptoxdog |
| tests/ci/ | CI contract enforcement tests | Platform | T2+ | @cryptoxdog |
| tools/ | Audit + verification scripts | Platform | T2+ | @cryptoxdog |
| tools/audit_engine.py | 27-rule audit engine | Platform | T2+ | @cryptoxdog |
| tools/verify_contracts.py | Contract verifier | Platform | T2+ | @cryptoxdog |
| chassis/ | Deprecated compatibility artifacts only | Platform | READ-ONLY unless migration review | @cryptoxdog |
| codegen/ | Code generation templates | Platform | T2+ | @cryptoxdog |
| config/ | Application config modules | Platform | T3+ | @cryptoxdog |
| domains/ | Domain spec YAML files | Domain teams | T3+ | @cryptoxdog |
| infra/ | Infrastructure-as-code | Platform | T3+ | @cryptoxdog |
| migrations/ | Alembic DB migrations | Platform | T3+ | @cryptoxdog |
| monitoring/ | Observability config | Platform | T2+ | @cryptoxdog |
| odoo_modules/ | Odoo ERP integration modules | Odoo team | T3+ | @cryptoxdog |
| reports/ | Generated audit/scan reports | Auto-generated | READ-ONLY | — |
| scripts/ | Deployment and utility scripts | Platform | T3+ | @cryptoxdog |
| templates/ | L9 template source files | Platform | T3+ | @cryptoxdog |
| readme/ | README assets | Any | T1+ | — |
| .github/ | CI workflows, CODEOWNERS, templates | Platform | T2+ | @cryptoxdog |
| .semgrep/ | Semgrep policy rules | Platform | T3+ | @cryptoxdog |

---

## Key Files

| File | Purpose | Agent Relevance |
|---|---|---|
| `app/main.py` | SDK runtime app entrypoint | Owns production `/v1/execute` ingress |
| `app/api/v1/chassis_endpoint.py` | Supplemental transport-adjacent routes | Keep separate from SDK runtime ingress |
| `app/services/chassis_handlers.py` | Supplemental SDK inbound handler registration | Align with runtime actions |
| `app/engines/orchestration_layer.py` | Canonical SDK action registration | Lockstep with handlers/runtime |
| `app/engines/handlers.py` | Action implementations | Must match registered runtime actions |
| `app/engines/graph_sync_client.py` | Gate-only outbound transport | No raw peer HTTP |
| `engine/utils/security.py` | sanitize_label() | Always use for Cypher labels |
| `tools/audit_engine.py` | 27-rule audit engine | Run via `make audit` |
| `tools/verify_contracts.py` | Contract verification | Run via `make verify` |
| `pyproject.toml` | Build + tool config | SSOT for ruff/mypy/pytest config |
| `.github/CODEOWNERS` | Required reviewer map | Route T4/T5 PRs accordingly |

---

## Deprecated Files

These files are intentionally excluded from the live transport constitution:

- `chassis/envelope.py`
- `chassis/router.py`
- `chassis/registry.py`

Do not use them as the source of truth for production runtime behavior.

---

## Test File Naming Convention

| Test Type | Path Pattern | Marker |
|---|---|---|
| Unit | tests/unit/test_*.py | @pytest.mark.unit |
| Integration | tests/integration/test_*.py | @pytest.mark.integration |
| Compliance | tests/compliance/test_*.py | (no extra marker) |
| CI contracts | tests/ci/test_*.py | (no extra marker) |
| Slow/convergence | Any | @pytest.mark.slow |

---

## Entry Points to Execution Flows

| Entry Point | Command | Flow |
|---|---|---|
| SDK transport runtime | `uvicorn app.main:app --host 0.0.0.0 --port 8000` | See EXECUTION_FLOWS.md Transport Execution Flow |
| Dev server | `make dev` | See EXECUTION_FLOWS.md Initialization Sequence |
| Audit | `make audit` | tools/audit_engine.py — standalone |
| Contract verify | `make verify` | tools/verify_contracts.py — standalone |
