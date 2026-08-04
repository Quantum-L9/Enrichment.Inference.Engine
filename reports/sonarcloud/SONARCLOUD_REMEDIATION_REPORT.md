# SonarCloud Remediation Report — Enrichment.Inference.Engine

## Executive verdict

**REMEDIATED_PENDING_REMOTE_ANALYSIS** — 33 confirmed issues fixed at their root cause
with minimal, behavior-preserving changes across 14 files. Remaining issues are either
proven false positives or deferred with explicit rationale (CI-install risk,
interface-contract async, or broad-refactor risk). Remote SonarCloud closure is PENDING
re-analysis of the candidate branch.

## Target identity

| Field | Value |
|---|---|
| Repository | `Quantum-L9/Enrichment.Inference.Engine` |
| SonarCloud org | `quantum-l9` |
| Project key | `Quantum-L9_Enrichment.Inference.Engine` |
| Analyzed branch | `main` (latest analysis) |
| Local HEAD (candidate base) | `5169e8f93d6335b4ffcd279040cfa1821849cf25` |
| Fix branch | `claude/enrichment-inference-engine-pr-7aii5x` |
| API access | anonymous public read (`/api/issues/search`, full pagination — 361/361) |

## Baseline quality gate

Quality gate: **ERROR** — failing condition `new_security_rating` actual `3` (threshold `≤1`).

| Metric | Value |
|---|---|
| Bugs | 9 |
| Vulnerabilities | 181 |
| Code smells | 171 |
| Security hotspots | 0 |
| Duplicated lines density | 0.8% |

## Issue summary by type / severity (361 total)

| Type | Count | | Severity | Count |
|---|---|---|---|---|
| VULNERABILITY | 181 | | BLOCKER | 6 |
| CODE_SMELL | 171 | | CRITICAL | 46 |
| BUG | 9 | | MAJOR | 270 |
| | | | MINOR | 39 |

## Root-cause clusters fixed (33 issues)

| # | Rule | Cnt | Files | Root cause → minimal fix |
|---|---|---|---|---|
| C1 | python:S930 (BUG/BLOCKER) | 4 | `app/api/v1/discover.py` | `/api/v1/scan` (the live route — intake router is not mounted) `await`-ed the **synchronous** `scan_crm_fields(crm_fields, domain_spec)` with 4 wrong kwargs and dict payloads → guaranteed 500. Rebuilt to construct `CRMField` objects, resolve `domain_spec` from the runtime registry (`converge._domain_specs`, mirroring `converge.scan_crm`), call the scanner synchronously, return `scan_result_to_dict(...)`; 404 on unknown domain. |
| C2 | python:S8572 | 11 | `graph_sync_hooks.py`, `crm/hubspot_client.py`, `crm/salesforce_client.py`, `engines/inference_bridge_v2.py` | `logging.error()` inside `except` handlers → `logging.exception()` (captures traceback); dropped now-redundant `exc_info=True`. |
| C3 | python:S7502 (BUG) | 2 | `engines/packet_router.py`, `services/event_emitter.py` | Fire-and-forget `asyncio.create_task(...)` result discarded → task could be GC'd mid-flight. Store strong ref in a set + `add_done_callback(discard)`. |
| C4 | pythonbugs:S2259 (BUG) | 1 | `services/event_emitter.py` | `self._nc.publish()` on a possibly-`None` connection. Narrowed via local `nc` + explicit `RuntimeError` guard after `connect()`. |
| C5 | python:S1244 (BUG) | 1 | `health/health_field_analyzer.py` | Float `stdev == 0.0` equality → `math.isclose(stdev, 0.0, abs_tol=1e-12)` (also guards near-zero division). |
| C6 | python:S9083 | 9 | `tests/test_crm_integration.py`, `tests/test_waterfall_enrichment.py` | Empty-paren pytest decorators → bare form, matching the repo-dominant style (62:3 fixture, 85:6 asyncio). |
| C7 | python:S1192 (CRITICAL) | 5 | `engines/convergence/schema_proposer.py` (×2), `score/score_api.py`, `core/telemetry.py`, `scripts/validate_phase5_readiness.py` | Duplicated string literals (≥3×) → module-level constants. |

## Confirmed vs rejected findings

**Rejected — false positive (no change, working code preserved):**

- `pythonbugs:S2583` × 1 — `app/services/crm/writeback.py:56` `if not client.connect()`.
  `CRMClientBase.connect()` and all implementations return a real `bool` (True/False
  branches present); Sonar's "always true" is a dataflow artifact from the polymorphic
  `self.client` attribute. Forcing a change would break correct code.

## Deferred with rationale (not fixed in this PR)

| Rule(s) | Cnt | Reason deferred |
|---|---|---|
| `python:S2245` | 37 | 36/37 in `app/services/simulation_bridge.py` — **non-security simulation randomness**. Replacing `random` with `secrets` is semantically wrong; only a suppression would clear it, which the remediation contract forbids for a non-proven case. FP candidate. |
| `python:S3776` | 38 | Cognitive-complexity refactors — broad behavioral surface; violates minimal-fix / no-broad-refactor contract. Separate refactor PR. |
| `python:S8415` | 27 | Additive FastAPI `responses=` metadata — safe but large surface; separate documentation PR. |
| `python:S1172` / `python:S7503` | 25 | Unused params / async-without-await are **interface-contract driven** (handler uniformity; `ARG00x` is ruff-ignored by repo policy). Removing them breaks the handler protocol. |
| `githubactions:S8544` / `S8541` | 89 | pip lock/`--only-binary` hardening — would break installs of the **git-based private SDK** (`constellation-node-sdk` @ Gate_SDK) and other sdist deps, breaking the very CI that must stay green. Needs a hashed lockfile (infra work). |
| `githubactions:S7637` / `S8233` / `S8264` / `S7636` / `S6573` | 34 | SHA-pinning + job-level permissions. Governance-aligned but mutates the workflows executing this PR's CI — isolated hardening PR to avoid pipeline risk. |
| `docker:*` / `kubernetes:*` / `shelldre:*` / `text:*` | ~28 | Deploy-manifest / container config — deployment-owner scope. |
| `pythonsecurity:S8705` / `S8707`, `python:S4790`, `python:S5332` | 15 | Agentic argument/path injection, weak hash, clear-text — require human security review before touching. |

## Issue → change traceability

See `sonarcloud-issues-before.json` for the full keyed issue set. Each fixed cluster maps
1:1 to the files in the "Root-cause clusters fixed" table above; every changed line
corresponds to a listed rule and cluster. No file was changed outside a mapped cluster.

## Validation results (candidate revision, local)

| Gate | Result | Notes |
|---|---|---|
| `ruff check` (14 changed files) | **PASS** | import order auto-fixed by ruff (its owned domain) |
| `ruff format --check` | **PASS** | all files already formatted |
| `python -m py_compile` (14 files) | **PASS** | syntax verified |
| `mypy` (changed files) | **PASS (no new errors)** | 7 pre-existing baseline `no-any-return`/`attr-defined` at untouched lines; mypy is advisory per `CLAUDE.md` |
| `pytest` (full suite) | **PENDING_REMOTE** | private SDK (`constellation-node-sdk` @ Gate_SDK) requires `SDK_TOKEN` unavailable locally; runs in CI. OpenAPI contract test for `/api/v1/scan` verified unaffected (path/method/operationId unchanged). |

## Remote analysis status

**PENDING** — SonarCloud has not analyzed the candidate branch. No remote issue closure is
claimed. Re-query `/api/issues/search` against the candidate revision after CI analysis to
confirm closure and quality-gate movement.

## Residual risks

- C1 couples `discover.py` to `converge._domain_specs` (the runtime spec registry). This is
  the existing single source of loaded specs; accessed lazily to avoid import cycles.
- Full behavioral verification of the SDK-dependent transport paths is deferred to CI.

## Next action

Merge-gate is `pr-pipeline.yml` (PR Pipeline Gate); SonarCloud is advisory. Push branch,
open PR, let CI run full tests + SonarCloud PR analysis, then reconcile remote closure.
