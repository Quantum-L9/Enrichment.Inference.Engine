# GitHub CI Recovery — Handoff (Resumed)

**Repo:** `Quantum-L9/Enrichment.Inference.Engine`
**Resumed:** 2026-06-30
**Method:** `l9-pr-remediation` skill run against all open PRs (#114, #122), followed by a
verified re-classification of every failing CI job.

This supersedes the prior handoff. It corrects three claims in the original that no longer
hold (see **Corrections**), and records the convergence result of the remediation loop.

---

## 0. Convergence result (l9-pr-remediation)

| Field | Value |
|-------|-------|
| `convergence_status` | **blocked — settings-gated** |
| Open PRs evaluated | #114, #122 |
| CI gates discovered | 21 workflow files parsed |
| Root-cause buckets | `SDK_TOKEN-403` (admin secret), `GITLEAKS_LICENSE` (admin secret/code), `registry-creds` (admin secret), `stale-base` (#114), `missing-pipeline-script` (code defect) |
| Code-fixable by this agent without new decisions | **0 blocking gates** (all blocking gates are settings-gated or require inventing tooling / a security-gate change — see §3) |
| `minimum_safe_next_action` | Set `SDK_TOKEN` org secret (admin), then re-run #122; rebase #114 onto `main` |

**Why blocked, not converged:** the gates that block merge are not defects in any PR diff.
The dominant blocker is a missing org-secret permission (`SDK_TOKEN` 403) that sinks ~10 jobs
on every PR. None of it is reachable from code in this repository.

---

## 1. Corrections to the prior handoff (verified against live logs)

1. **gitleaks "pin to v2 (free)" is INVALID.** `.github/workflows/gitleaks.yml` is *already*
   pinned to `gitleaks/gitleaks-action@v2.3.6` (`961680e…`), and the live run still fails:
   `[Quantum-L9] is an organization. License key is required.`
   `gitleaks-action` requires an org license on **v2 and v3**. The only real fixes are:
   (a) add the `GITLEAKS_LICENSE` org secret, or (b) replace the action with the standalone
   `gitleaks` **binary CLI** in a `run:` step (no license gate). Pinning the version does nothing.

2. **PR #114's red checks are STALE, not real.** That run is from 2026-06-25, *before* #121
   merged. Its failures — `SALESFORCE_CLIENT_ID not documented in env-contract.yaml`
   (Select and Run Gates) and `Blocking findings: 7` (L9 Audit Review) — are exactly the
   defects #121 fixed on `main`. **Action: rebase #114 onto current `main` and re-run.**
   Its remaining reds after rebase will be the same settings items (SDK_TOKEN / gitleaks /
   Build & Push).

3. **There is an additional, undocumented code defect class: missing pipeline scripts.**
   Six scripts referenced by workflows do not exist and never existed in git history:
   | Script | Referenced at | Guarded? |
   |--------|---------------|----------|
   | `local_pr_pipeline/docs_consistency_local.sh` | `pr-pipeline.yml:517` | ❌ hard-fail (this is the live **Docs Consistency** failure, exit 127) |
   | `local_pr_pipeline/docs_link_check_local.py` | `pr-pipeline.yml:521` | ❌ hard-fail |
   | `local_pr_pipeline/run_pr_select_gates.py` | `pr-pipeline.yml:487` | ❌ hard-fail |
   | `local_pr_pipeline/pr_pipeline.sh` | `pr-pipeline.yml:14,17` (doc refs) | n/a |
   | `local_pr_pipeline/check_compliance_terminology.py` | workflow ref | ❌ hard-fail |
   | `local_pr_pipeline/contract_bound_local.py` | `pr-pipeline.yml:495` | ✅ advisory (`|| echo`) |
   These were **not** part of any open PR's diff. Reconstructing them is net-new tooling, so it
   is deferred for an explicit decision (see §3, item D) rather than invented blind.

---

## 2. Per-job classification (the actual remediation triage)

### PR #122 — `claude/enrichment-inference-remediation-hmq0as` (head `5965f34`)

The SDK wiring in this PR is **correct** — logs show `SDK_TOKEN: ***` injected and the clone
hitting `https://github.com/Quantum-L9/Gate_SDK.git`. The only failure is the 403 (permission).

| Failing job | Root cause | Fixable in code? |
|-------------|-----------|------------------|
| Lint (Ruff + Mypy) | SDK_TOKEN 403 on `pip install -r requirements-ci.txt` | No — org secret |
| Tier 2 Constitution + Attestation Tests | SDK_TOKEN 403 | No — org secret |
| Constitution + Attestation | SDK_TOKEN 403 | No — org secret |
| Architecture Compliance | SDK_TOKEN 403 | No — org secret |
| L9 Constitution Gate | SDK_TOKEN 403 | No — org secret |
| Semgrep Policy Check | SDK_TOKEN 403 | No — org secret |
| Security Scanning | SDK_TOKEN 403 | No — org secret |
| CycloneDX SBOM (SOC 2 CC9.2) | SDK_TOKEN 403 | No — org secret |
| CI Gate / PR Pipeline Gate | aggregate of the above | No — downstream |
| Secret scanning (SOC 2 CC6.1) | `GITLEAKS_LICENSE` unset | Secret (admin) OR binary swap (code) |
| Docs Consistency | missing `docs_consistency_local.sh` (exit 127) | Code, but tooling never existed — see §3 D |
| Build & Push Image | container-registry creds | No — admin secret |

Passing: Validate (syntax+YAML), Constitution Verify, Dependency Review, Secret scan,
License Compliance, CodeQL, Contract-Bound Change Gate.

### PR #114 — `claude/zen-sagan-u8s67s` (head `eb28c67`)

Recovered rate-limiter fix (`app/middleware/rate_limiter.py` → returns `JSONResponse` 429 +
`tests/test_rate_limiter.py`). No review threads open. Red checks are **stale (pre-#121)**.

| Failing job | Root cause | Fix |
|-------------|-----------|-----|
| Select and Run Gates | `SALESFORCE_CLIENT_ID` env-contract — fixed by #121 | Rebase onto `main` |
| L9 Audit Review | `Blocking findings: 7` — audit false-positive fixed by #121 | Rebase onto `main` |
| CI Gate / PR Pipeline Gate | aggregate | Rebase onto `main` |
| Secret scanning (SOC 2 CC6.1) | `GITLEAKS_LICENSE` unset | Secret (admin) OR binary swap |
| Build & Push Image | registry creds | Admin secret |

---

## 3. Remaining actions, in priority order

**Admin / settings (cannot be done in code — same as original handoff):**

- **A. `SDK_TOKEN` org secret with `Contents:Read` on `Quantum-L9/Gate_SDK`** — highest
  priority; unblocks ~10 jobs on every PR. Fine-grained PAT (resource owner `Quantum-L9`,
  `Gate_SDK`, Contents: Read-only) or org GitHub-App token, stored at
  `https://github.com/organizations/Quantum-L9/settings/secrets/actions`.
- **B. `GITLEAKS_LICENSE` org secret** — free org key from https://gitleaks.io. (Pinning the
  action version does **not** help — see Correction 1.)
- **C. Build & Push registry creds** (GHCR `packages: write`, or registry secrets), or mark the
  job non-blocking if it is infra-only.
- **E. OpenSSF Scorecard** — confirm `repo_token`/`permissions:` for the new org, or scope it
  advisory (schedule-only, not on PRs).
- **F. (optional) Enable Issues** so deferred items can be tracked as real issues.

**Code-fixable, but each needs an explicit go-ahead (held, not invented blind):**

- **D. Missing pipeline scripts** (`docs_consistency_local.sh`, `docs_link_check_local.py`,
  `run_pr_select_gates.py`, `check_compliance_terminology.py`). These never existed; authoring
  them defines new gate behavior. Decision needed: reconstruct best-effort, or make the
  workflow steps tolerant (guard the missing-script steps `|| echo advisory`) until the real
  scripts are ported from the source template.
- **G. gitleaks binary swap** (alternative to B): replace `gitleaks-action` with the standalone
  `gitleaks` CLI in a `run:` step to drop the license dependency entirely.

**Git hygiene (needs permission to push to the PR branches):**

- **H. Rebase #114 onto current `main`** to clear its stale (already-fixed) failures.
- **I. Re-run #122** once **A** is set; merge when green.

---

## 4. Definition of done

- [ ] **A** — `SDK_TOKEN` grants `Contents:Read` on `Quantum-L9/Gate_SDK`; #122 re-run green → merge #122
- [ ] **B/G** — `GITLEAKS_LICENSE` set, **or** gitleaks swapped to the binary CLI (v2-pin is not an option)
- [ ] **C** — Build & Push green or scoped infra-only
- [ ] **D** — missing pipeline scripts reconstructed or their steps guarded
- [ ] **E** — Scorecard green or scoped advisory
- [ ] **H** — #114 rebased onto `main`, re-run, remaining reds are settings-only
- [ ] **F** — (optional) Issues enabled; tracking issue filed
