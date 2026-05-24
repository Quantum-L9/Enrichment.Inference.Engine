# CI/CD Pipeline — Enrichment.Inference.Engine

**Version:** 1.4.0
**Date:** 2026-05-24
**Status:** Active
**Sibling Parity:** Cognitive.Engine.Graphs

---

## Overview

This document describes the production-grade CI/CD pipeline for the Enrichment.Inference.Engine. The pipeline enforces code quality, security, architecture compliance, and deployment automation as a unified governance layer. It is designed for **sibling uniformity** with the Cognitive.Engine.Graphs pipeline so that all L9 constellation nodes share identical engineering standards.

### Local `make pr` ↔ GitHub `pr-pipeline.yml` Parity

**Local** and **GitHub** pipelines are now fully synchronized:

| Local | GitHub | Description |
|-------|--------|-------------|
| `make pr` | `pr-pipeline.yml` | Full 8-phase gate (validate → lint → semgrep → test → security → compliance → L9 → docs) |
| `make pr-quick` | `workflow_dispatch` with skip flags | Minimal checks (no integration tests, minimal L9) |

Run **`make pr`** before opening a PR for local validation. The **`pr-pipeline.yml`** is the **intended canonical merge gate** for this repository. Its enforcement in branch protection is **Unknown** — verify in Settings → Branches before relying on it as a hard block.

**Environment variables (local):**
- `PR_SKIP_INTEGRATION=1` — Skip pytest with Postgres/Redis
- `PR_SKIP_SEMGREP=1` — Skip Semgrep policy checks
- `PR_SKIP_L9=1` — Skip L9 constitution/contract checks
- `PR_L9_MINIMAL=1` — Minimal L9 (no select-gates runner)
- `PR_MYPY_STRICT=1` — Fail on mypy errors (non-blocking by default)
- `PR_SECURITY_STRICT=1` — Fail on security warnings
- `PR_SKIP_GITLEAKS=1` — Skip Gitleaks secret detection
- `ORDER=gate|failfast` — Phase execution order

**GitHub workflow inputs (manual dispatch):**
- `skip_integration` — Skip integration tests
- `skip_semgrep` — Skip Semgrep
- `skip_l9` — Skip L9 checks
- `l9_minimal` — Minimal L9 only
- `mypy_strict` — Fail on mypy errors
- `security_strict` — Fail on security warnings

See `local_pr_pipeline/pr_pipeline.sh` for implementation details.

---

## Pipeline Architecture

### Workflow Summary

| Workflow | Trigger | Purpose | Blocking |
|----------|---------|---------|----------|
| **`pr-pipeline.yml`** | **PR, Push, Manual** | **Full PR gate (8 phases) — parity with `make pr`** | **Intended yes; enforcement Unknown — see branch protection** |
| `ci.yml` | Push, PR | Main pipeline (validate, lint, semgrep, test, security, SBOM, scorecard) | Yes |
| `compliance.yml` | PR (Python changes) | Architecture compliance (terminology, chassis isolation, KB schema, L9 contracts) | Yes |
| `supply-chain.yml` | PR, push, weekly | License compliance, dependency review | Partial |
| `codeql.yml` | PR, push, weekly | GitHub CodeQL semantic analysis | No |
| `docker-build.yml` | Push to main, tags | Build, scan, sign Docker images | No |
| `k8s-deploy.yml` | Manual dispatch | Helm-based K8s deployment with auto-rollback | No |
| `pr-review-enforcement.yml` | PR opened/sync | PR size limits and review policy | Yes (>1000 lines) |
| `refactoring-validation.yml` | refactor/* branches | Hard gate for refactoring PRs | Yes |
| `release-drafter.yml` | Push to main | Auto-draft release notes | No |
| `docs-sync.yml` | Docs changes | Validate documentation links | No |
| `docs-consistency.yml` | Docs / governance | ADR + INVARIANTS + governance markdown | Yes (if required) |
| `l9-constitution-gate.yml` | PR | Constitution + tier2 contract tests + optional PR-bound diff | Yes (if required) |
| `l9-contract-control.yml` | PR | `l9_contract_control.py` verify / select-gates | Yes (if required) |
| `release.yml` | Tags / manual | Releases | Varies |
| `perplexity-code-review.yml` | PR (optional) | Perplexity-assisted review | No |
| `sonarcloud.yml` | Push, PR | SonarQube Cloud analysis + coverage | No (skips if unset) |
| `gitguardian.yml` | Push, PR | GitGuardian ggshield secret scan | No (skips if no API key) |
| `coderabbit-notify.yml` | PR review | CodeRabbit review notifications | No |

### PR Pipeline Phases (pr-pipeline.yml)

The **`pr-pipeline.yml`** workflow is the primary code review gate. It runs 8 parallel/sequential phases that mirror `make pr`:

```
validate → lint ──────┐
    │                 │
    ├─→ semgrep ──────┤
    │                 │
    ├─→ test ─────────┤
    │                 │
    ├─→ security ─────┼─→ pr-pipeline-gate (fan-in)
    │                 │
    ├─→ compliance ───┤
    │                 │
    ├─→ l9 ───────────┤
    │                 │
    └─→ docs ─────────┘

Phase Details:
  validate   — Python syntax + workflow YAML + KB YAML
  lint       — Ruff check + format + MyPy type check
  semgrep    — Semgrep policy rules (.semgrep/)
  test       — pytest with coverage (Postgres + Redis services)
  security   — Gitleaks + pip-audit + Safety + Bandit + dependency-review
  compliance — Terminology guard + chassis isolation + KB schema + L9 contracts
  l9         — Constitution verify + tier2 tests + attestation + select-gates
  docs       — Docs consistency + markdown link check
```

The **`pr-pipeline-gate`** job posts a summary comment on the PR with pass/fail status for each phase.

### CI Pipeline Phases (ci.yml)

```
validate → lint → semgrep → test → security → sbom → scorecard → ci-gate
    │         │       │        │        │        │        │          │
    │         │       │        │        │        │        │          └─ Fan-in: PASS/FAIL
    │         │       │        │        │        │        └─ OpenSSF Scorecard
    │         │       │        │        │        └─ SPDX SBOM generation
    │         │       │        │        └─ Gitleaks + pip-audit + Safety + Bandit
    │         │       │        └─ pytest with coverage (Postgres + Redis services)
    │         │       └─ Semgrep policy rules (.semgrep/)
    │         └─ Ruff lint + format + MyPy type check
    └─ Python syntax + YAML validation + KB schema
```

**Note:** `ci-quality.yml` and `test.yml` were consolidated into `ci.yml` to eliminate redundant test runs. The `pr-pipeline.yml` workflow provides a unified gate that combines CI + compliance + L9 + docs checks.

---

## Pre-Commit Hooks

Local enforcement runs before code reaches the remote pipeline:

| Hook | Stage | Purpose |
|------|-------|---------|
| `ruff` | commit | Auto-fix lint issues |
| `ruff-format` | commit | Auto-format code |
| `gitleaks` | commit | Block committed secrets |
| `mypy` | commit | Type checking on `app/` |
| `pytest-unit` | commit | Fast unit test subset |
| `block-fastapi-in-engine` | commit | Chassis isolation enforcement |
| `l9-contract-audit` | commit | L9 contract compliance |
| `l9-contract-verify` | push | L9 contract file verification |
| `terminology-guard` | commit | Canonical term enforcement |
| `check-yaml` | commit | YAML validation |
| `end-of-file-fixer` | commit | POSIX compliance |
| `trailing-whitespace` | commit | Clean whitespace |
| `check-added-large-files` | commit | Block files >1MB |

### Installation

```bash
pip install pre-commit
pre-commit install
pre-commit install --hook-type pre-push
```

---

## Configuration

All workflows use **zero-config defaults** that work immediately. Customize via GitHub Repository Variables (Settings → Variables → Actions). See `.github/env.template` for the full list.

### Key Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PYTHON_VERSION` | `3.12` | Python version for CI |
| `SOURCE_DIR` | `app` | Source directory for coverage and type checks |
| `COVERAGE_THRESHOLD` | `60` | Minimum test coverage percentage |
| `PR_WARN_LINES` | `300` | PR size warning threshold |
| `PR_BLOCK_LINES` | `1000` | PR size blocking threshold |

### Active CI Secrets

See `docs/contracts/config/env-contract.yaml` → `github_actions.secrets` for the authoritative contract.
All active CI secrets are platform-wide (org-level) and require no per-repo secret configuration beyond granting repository access.

| Secret | Scope | Required For |
|--------|-------|-------------|
| `GITGUARDIAN_API_KEY` | platform_wide | GitGuardian ggshield secret scan |
| `SONAR_TOKEN` | platform_wide | SonarCloud analysis |
| `CODECOV_TOKEN` | platform_wide | Coverage upload |
| `PERPLEXITY_API_KEY` | platform_wide | Perplexity PR review |

---

## Sibling Parity

This pipeline is structurally identical to `Cognitive.Engine.Graphs` with engine-specific adaptations:

| Aspect | Graph Engine | Enrichment Engine |
|--------|-------------|-------------------|
| Source directory | `engine/` | `app/` |
| Compliance checks | Cypher injection guard | Chassis isolation guard |
| KB validation | N/A | YAML rule schema validation |
| L9_META engine tag | `graph` | `enrichment` |
| Tool versions | Shared via `requirements-ci.txt` | Same pinned versions |

---

## Deployment

### Kubernetes (Production)

```bash
# Trigger via GitHub Actions UI
# Workflow: Kubernetes Deployment
# Select environment: staging or production
```

### Docker Build

Docker images are automatically built on push to `main` and tagged releases. Images are pushed to `ghcr.io` with multi-tag strategy (branch, SHA, semver, latest).

---

## Troubleshooting

**CI Gate fails on lint:** Run `ruff check . --fix && ruff format .` locally before pushing.

**Coverage below threshold:** Add tests for uncovered modules. Check `pytest --cov=app --cov-report=term-missing` locally.

**Pre-commit hooks fail:** Run `pre-commit run --all-files` to see all issues. Fix and re-commit.

**Architecture compliance fails:** Check for FastAPI imports outside `app/api/` and `app/main.py`. Engine modules must remain chassis-agnostic.
