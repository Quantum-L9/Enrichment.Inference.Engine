---
name: EIE CI and compliance catch-up to CEG parity
overview: "Bring Quantum-L9/Enrichment.Inference.Engine main to CEG's CI/compliance level. The required org check Analyze (central Core) must be green with no false-positive-driven verdicts. main's Vulnerability Scan and OpenSSF Scorecard must be green. EIE must hold zero toolchain mismatches against Core, run hash-locked CI installs with a deterministic lock gate, isolate the networked SDK channel check,..."
todos:
  - id: T1
    content: "l9-ci-sdk PR: precision for the token regex (exclude count/tokenize forms, keep credential *_tokens), constant-only sql-string-format (flag only non-UPPER_CASE interpolations), receiver-scoped routing and raw-execute rules (L9 node/peer/gate sources only), and a handler-name requirement for raw-payload-parameter. Keep canonical_rule_ids, rule ids and L9_META headers; add TP/TN fixture lines and ADR-0019"
    status: pending
    phase: execute
    depends_on: []
  - id: T2
    content: "l9-ci-core PR: semgrep-policy.yaml metadata.version 4, classify insecure-file-permissions as advisory and extend the note with the numeric-mode reason (0o700 owner-only reported as permissive)"
    status: pending
    phase: execute
    depends_on: []
  - id: T3
    content: "l9-ci-core step 1: promote the T1 merge SHA in .l9/sdk-compatibility.yaml (keep 6d70e829 as rollback), update every sdk-pin-mirrors path, regenerate the provision-sdk runtime lock with lock_runtime.py, update tests/provisioning"
    status: pending
    phase: execute
    depends_on: [T1]
  - id: T4
    content: "l9-ci-core step 2: advance org-ci.yml Core action pins to the step-1 commit and its sdk-revision fallback to the T1 SHA; prove with tools/advance_core_action_pins.py <sha> --check; add the dated AGENTS.md activation section (append-only)"
    status: pending
    phase: execute
    depends_on: [T2, T3]
  - id: T5
    content: "EIE PR-1: docker-build.yml add `actions: read`; ci.yml scorecard job add issues/pull-requests/checks read (CEG #416); delete .github/workflows/baseline-ratchet-caller.yml (ADR-0016); git rm the 16 tracked `Current Work/` files (already in .gitignore)"
    status: pending
    phase: execute
    depends_on: []
  - id: T6
    content: "EIE PR-2: ruff 0.16.1 / mypy 2.3.0 / pytest 9.1.1 in pyproject [dev], requirements-ci.txt and pre-commit; pre-commit revs by SHA with tag comments; mirrors-mypy additional_dependencies equal to the CI type-check deps; pytest-unit hook drops `2>/dev/null || true`; Dependabot pip ignore for ruff/mypy/pytest (Core-owned); requires-python >=3.12 (CEG #409)"
    status: pending
    phase: execute
    depends_on: []
  - id: T7
    content: "EIE PR-3: split the networked `validate_sdk_pin.py --verify-tag` step out of validate into an sdk-channel job that CI Gate reports but validate no longer waits on (CEG #412); keep the offline pin check in validate"
    status: pending
    phase: execute
    depends_on: [T5]
  - id: T8
    content: "EIE PR-4: tools/lock_requirements.sh renders seeded from the committed lock with a recorded --exclude-newer cutoff (tools/deps/exclude-newer.txt) and pinned uv; add --check (exit 1 on drift) plus a requirements-ci.lock hashed render; add scripts/install_locked.sh (--require-hashes) and switch all 13 requirements installs plus the pr-pipeline git+@v1 SDK install; add a lock --check step to sdk-channel; add a unit test proving --check fails on a stale lock (CEG #394/#419)"
    status: pending
    phase: execute
    depends_on: [T6, T7]
  - id: T9
    content: "EIE PR-5: pin the CI Semgrep install; port CEG #418 precise float/singleton rules into .semgrep/semgrep-rules.yaml with fixtures under .semgrep/fixtures/, a .semgrepignore and a semgrep --test CI step"
    status: pending
    phase: execute
    depends_on: [T8]
  - id: T10
    content: "EIE PR-6: SHA-pin all 96 tag-pinned workflow `uses:` refs with `# vX` comments; the Dependabot github-actions ecosystem keeps them current"
    status: pending
    phase: execute
    depends_on: [T7, T8, T9]
  - id: T11
    content: "EIE PR-7, BLOCKED until explicit operator APPROVED (rules 90/93, Docker/deploy protection): docker-build.yml build-push-action `file: Dockerfile.prod`, so CI publishes the digest-pinned, hash-locked, non-root production image instead of the DEV image"
    status: pending
    phase: execute
    depends_on: [T5, T8]
  - id: T12
    content: "Ledger: record session_debt findings for the CEG follow-ups (docker-build actions: read; dead baseline-ratchet caller), EIE V2 contract adoption, SonarCloud residual (Dockerfile + text:S8565) after PR-4, and Dockerfile curl/git runtime removal pending APPROVED"
    status: pending
    phase: execute
    depends_on: [T8]
isProject: false
kind: simple
execute_via: cursor-build
status: current
---

# PLAN: EIE CI and compliance catch-up to CEG parity

> **Projected by** `scripts/render_plan_pe_autonomy.py` from validated PLAN_DOCUMENT JSON.

> **First-class SSOT (git):** `environment/contracts/execution/templates/canonical.template.executable_plan.v1.plan.md` · metadata sidecar `*.meta.md` · registered in `environment/contracts/execution/MANIFEST.yaml`. Skill path is a symlink; `.cursor/plans/_TEMPLATE.plan.md` is a local mirror only.
> **Schema:** `canonical.schema.plan_document.v1` (status: fill → `executable` only when law holds)
> **Parameters:** every value is projected from PLAN_DOCUMENT. A value reading `unknown — …` or `unbound — …` is absent from PLAN_DOCUMENT and must be resolved before status becomes `executable`.
> **Execute:** when status is `executable`, press **Build**, stack on the unique open-PR tip if any open PR exists (`PR_STACK=auto`; never `origin/main`), then `PR_STACK=auto PR_REMEDIATE=0 make pr` and display the PR URL. Do **not** run `make campaign`, admit a Program Lock, or free-form mutate from this markdown alone.
> **Cursor todos:** frontmatter `todos` project to Build todos. Body is the binding contract.
> **File name:** `eie_ci_and_compliance_catch_up_to_ceg_parity_66d7db64.plan.md`
> **Law:** executable only when baseline matches, capability probes pass, invariants match, and envelope is respected. Markdown completeness alone is insufficient.

## Execute via Cursor Build

Press **Build**. Plan on the current workspace. Execute on the unique open-PR chain tip.

- If any open PR exists: **never** branch from `origin/main`. Start from the unique chain tip (`PR_STACK=auto`). Use `agent_worktree_start.sh` when this checkout is not already that tip. Sibling open-PR chains fail closed.
- If the board is empty: `origin/main` is allowed.
- Do not run `make campaign`.
- Do not admit a Program Lock or Controller lease.
- Do not write `Lock: origin/main = <sha>`.
- Do not open a new worktree from tip as a **planning** requirement.
- After Build todos complete: scoped-commit (pathspecs), `l4_local.py authorize-release`, then `PR_STACK=auto PR_REMEDIATE=0 make pr`. Do not skip `make pr`.
- The finish reply **must** display the opened PR URL as proof. Without that URL the Build is incomplete.

## Metadata

| Field | Value |
|-------|-------|
| plan_id | `plan.simple.eie_ci_and_compliance_catch_up_to_ceg_parity.v1` |
| name | EIE CI and compliance catch-up to CEG parity |
| overview | Bring Quantum-L9/Enrichment.Inference.Engine main to CEG's CI/compliance level. The required org check Analyze (central Core) must be green with no false-positive-driven verdicts. main's Vulnerability Scan and OpenSSF Scorecard must be green. EIE must hold zero toolchain mismatches against Core, run hash-locked CI installs with a deterministic lock gate, isolate the networked SDK channel check, use precise pinned local Semgrep rules, SHA-pin its workflow actions, and drop dead workflows and scratch. All of this lands through fixes at each instrument's owner (l9-ci-sdk, l9-ci-core, EIE CI files) and never through EIE application-code edits or suppressions. |
| schema_version | `1.0.0` |
| status | `draft` — planning convergence is `partial` |
| is_project | `false` |
| owner | igor_beylin |
| created_at | 2026-11-06T04:19:11+00:00 |
| updated_at | 2026-11-06T04:19:11+00:00 |
| depth | `deep` |
| estimate | 11 PRs across 3 repos (4 upstream, 7 EIE), about 2 working sessions |

## Architect framing

| Field | Value |
|-------|-------|
| planning_ssot | validated PLAN_DOCUMENT JSON (`validate_plan_document.py`) |
| plan_class | `bounded_execution_contract` — PLAN_DOCUMENT mode `plan` |
| redesign_allowed | `false` |
| follow_on_schema_evolution_separate | `true` |
| framing_notes | Execute via Cursor Build; stacked make pr if any open PR exists; no redesign unless plan_class requires it |

## Immutable baseline

| Field | Value |
|-------|-------|
| captured_at | `2026-11-06T04:19:11+00:00` |
| repository | `Quantum-L9/Enrichment.Inference.Engine` |
| workspace | `/home/user/eie-plan` |
| branch | `agent/claude-code/eie-ci-catchup-plan` |
| commit_sha | `12d8b351b3c804094d3fe539ea578ea946da494b` |
| dirty | `false` |
| artifact_hashes | `{}` — none pinned by PLAN_DOCUMENT |
| allowed_local_dirt | none |
| overlap_policy | `stop_if_dirty_overlaps_may_modify` |
| verification_rule | `reverify_at_execution_start` |
| on_drift | `stop_and_replan` |

## Objective

### Mission

Bring Quantum-L9/Enrichment.Inference.Engine main to CEG's CI/compliance level. The required org check Analyze (central Core) must be green with no false-positive-driven verdicts. main's Vulnerability Scan and OpenSSF Scorecard must be green. EIE must hold zero toolchain mismatches against Core, run hash-locked CI installs with a deterministic lock gate, isolate the networked SDK channel check, use precise pinned local Semgrep rules, SHA-pin its workflow actions, and drop dead workflows and scratch. All of this lands through fixes at each instrument's owner (l9-ci-sdk, l9-ci-core, EIE CI files) and never through EIE application-code edits or suppressions.

### Scope in

- U1 (l9-ci-sdk): rule precision for AST-LOGGING forbidden-secret-field/keyword token regex, baseline sql-string-format constant-only interpolations, routing httpx/requests direct-node-dispatch and transport raw-execute-dict-contract receiver scoping, handler raw-payload-parameter handler-name requirement; fixtures and an ADR
- U2 (l9-ci-core): semgrep-policy.yaml v4 classifies python.lang.security.audit.insecure-file-permissions advisory, with a note
- U3 (l9-ci-core): step 1 SDK promotion in .l9/sdk-compatibility.yaml plus all sdk-pin-mirrors paths and tests/provisioning
- U4 (l9-ci-core): step 2 advances org-ci.yml Core action pins and the sdk-revision fallback
- EIE PR-1: workflow permissions (docker-build actions: read; Scorecard issues/pull-requests/checks read), delete baseline-ratchet-caller.yml, delete the 16 tracked `Current Work/` files
- EIE PR-2: toolchain to Core pins (ruff 0.16.1, mypy 2.3.0, pytest 9.1.1), SHA-pinned pre-commit revs, mirrors-mypy deps aligned with CI, the pytest-unit hook made able to fail, a Dependabot ignore for Core-owned tools, requires-python >=3.12
- EIE PR-3: move scripts/validate_sdk_pin.py --verify-tag out of the validate job into a networked sdk-channel job
- EIE PR-4: deterministic tools/lock_requirements.sh (committed-lock seed, recorded --exclude-newer, --check), a hashed requirements-ci.lock, scripts/install_locked.sh, all CI installs switched, --check in sdk-channel
- EIE PR-5: pinned local Semgrep plus CEG #418 precise rules with fixtures and a semgrep --test CI step
- EIE PR-6: SHA-pin every workflow action ref
- EIE PR-7 (only after explicit operator APPROVED): docker-build.yml builds Dockerfile.prod

### Success properties

Each property's proof is the `final_validation` entry it names (by id or by script/file). A property with no proving validation, or only skipped / not_applicable ones, blocks `executable`.

| id | property | evidence_type | proof | blocking |
|----|----------|---------------|-------|----------|
| SP-01 | V1: l9-ci-sdk precision PR merged; SDK self-CI green; semgrep --test over tests/fixtures/semgrep/rules/python passes, including the new true-positive and true-negative lines | `quality_gate` | V1: `cd l9-ci-sdk && ruff check . && ruff format --check . && mypy l9_ci && pytest -q && semgrep --test --config l9_ci/rulesets/semgrep/python tests/fixtures/semgrep/rules/python` → all pass, including new TP/TN lines | `true` |
| SP-02 | V2: ci-parity semgrep-l9 lane on EIE main with the promoted SDK reports blocking=0 (baseline 27 at 12d8b351) | `quality_gate` | V2: `python3 /root/.cursor-governance/ops/ci_parity/run.py --gate HEAD --workspace <eie> --base <sha> --lanes semgrep-l9 (with promoted SDK)` → blocking=0 | `true` |
| SP-03 | V3: Core step 1 and step 2 merged; `python3 tools/advance_core_action_pins.py <sha> --check` passes; Core self-CI green | `quality_gate` | V3: `cd l9-ci-core && python3 tools/advance_core_action_pins.py <sha> --check && pytest -q tests/provisioning` → pins proven; provisioning tests pass; Core self-CI green | `true` |
| SP-04 | V4: Analyze (central Core) success on an EIE PR head after V3 | `quality_gate` | V3: `cd l9-ci-core && python3 tools/advance_core_action_pins.py <sha> --check && pytest -q tests/provisioning` → pins proven; provisioning tests pass; Core self-CI green; V4: `gh api repos/Quantum-L9/Enrichment.Inference.Engine/commits/<pr-head>/check-runs (Analyze (central Core))` → conclusion success | `true` |
| SP-05 | V5: first EIE main push after PR-1 shows Vulnerability Scan and OpenSSF Scorecard success; baseline-ratchet-caller.yml no longer runs | `quality_gate` | V5: `gh api repos/Quantum-L9/Enrichment.Inference.Engine/commits/<main-sha>/check-runs (Vulnerability Scan, OpenSSF Scorecard)` → both success; no baseline-ratchet run | `true` |
| SP-06 | V6: central toolchain-alignment step on EIE reports 0 mismatches (baseline 8) | `quality_gate` | V6: `toolchain-alignment step output in Analyze (central Core) on EIE` → 0 mismatches | `true` |
| SP-07 | V7: lock --check exits 0 on main and 1 on a stale-lock negative-control test; every CI Python install uses --require-hashes | `quality_gate` | V7: `bash tools/lock_requirements.sh --check && pytest -q tests/unit/test_requirement_locks.py && ! grep -rnE 'pip install (-r requirements\|.*git\+)' .github/workflows` → --check exit 0 on main; negative-control test passes; no unhashed install remains | `true` |
| SP-08 | V8: EIE pytest suite passes with no new skip or xfail (baseline 3032 passed, 25 skipped, 4 xfailed) | `quality_gate` | V8: `pytest -q (EIE full suite as CI runs it)` → >=3032 passed, skipped <=25, xfailed <=4, 0 failed | `true` |
| SP-09 | V9: pinned local semgrep --test passes on the EIE fixture; semgrep --config .semgrep --error . reports 0 | `quality_gate` | V9: `semgrep --test --config .semgrep/semgrep-rules.yaml .semgrep/fixtures/semgrep-rules.py && semgrep --config .semgrep --error .` → fixture pass; 0 findings | `true` |
| SP-10 | V10: 0 workflow `uses:` refs pinned by mutable tag (baseline 96) | `quality_gate` | V10: `grep -hoE 'uses: [^ ]+@[^ ]+' .github/workflows/*.yml \| grep -vcE '@[0-9a-f]{40}'` → 0 | `true` |
| SP-11 | V11: SonarCloud new-code S8541/S8544 count 0 after PR-4 (baseline ~95) | `quality_gate` | V11: `SonarCloud api/issues/search rules=githubactions:S8541,githubactions:S8544 on main` → 0 open | `true` |
| SP-12 | V12: every EIE PR passes the .pre-commit-config.yaml hook catalog on its diff | `quality_gate` | V12: `pre-commit run --config .pre-commit-config.yaml --from-ref origin/main --to-ref HEAD (each EIE PR)` → all hooks pass, including the now-failing-capable pytest-unit hook | `true` |

## Capability preflight

`schema_ref:` `canonical.schema.capability_preflight.v1`
`instance_binding:` `capability_preflight_ref` → `preflight.plan.simple.eie_ci_and_compliance_catch_up_to_ceg_parity.v1`

| Field | Value |
|-------|-------|
| preflight_id | `preflight.plan.simple.eie_ci_and_compliance_catch_up_to_ceg_parity.v1` |
| source_ref | `plan.simple.eie_ci_and_compliance_catch_up_to_ceg_parity.v1` |
| phase_id | `preflight` |
| blocking | `true` |
| immutable_baseline_ref | `baseline.plan.simple.eie_ci_and_compliance_catch_up_to_ceg_parity.v1` |
| baseline_verified | `false` — reverify at execution start |
| drift_detected | `false` — baseline captured at render |

### Probes (from PLAN_DOCUMENT `pre_validation`; failed blocking probe → status `preflight_blocked`)

| id | capability | command_or_action | pass_criteria | blocking |
|----|------------|-------------------|---------------|----------|
| P0 | pre-validation (status `passed`) | `bind workspace /home/user/eie-plan, branch agent/claude-code/eie-ci-catchup-plan at origin/main 12d8b351b3c804094d3fe539ea578ea946da494b; hook catalog .pre-commit-config.yaml` | single repository root; HEAD equals fetched origin/main; clean tree | `true` |
| P1 | pre-validation (status `passed`) | `ci-parity gate HEAD --lanes semgrep-l9 on EIE 12d8b351 (SDK 6d70e829, semgrep 1.171.0, Core policy v3)` | blocking=27, every finding classified false positive with file:line | `true` |
| P2 | pre-validation (status `passed`) | `prototype SDK ruleset (scratchpad eie-sg/rules-fixed) across EIE, CEG, l9-ci-core, l9-ci-sdk, l9-constellation-topology plus the SDK fixture suite` | EIE 27 to 0 (with Current Work removed for its 2), other repos 0 delta, all SDK fixtures pass | `true` |
| P3 | pre-validation (status `passed`) | `read docker-build.yml permissions, the ci.yml scorecard job, scripts/docker-entrypoint.sh, Dockerfile.prod, Core AGENTS.md §16/§19 and .l9/core-repo-policy.json sdk-pin-mirrors` | the missing permissions, the DEV-image publication and the two-step promotion procedure are confirmed | `true` |
| P4 | pre-validation (status `passed`) | `ruff 0.16.1 check + format --check and mypy 2.3.0 on EIE` | clean, no code churn from the pin move | `true` |
| P5 | pre-validation (status `passed`) | `gh api repos/Quantum-L9/Enrichment.Inference.Engine (merge methods) + branch rules` | squash allowed; linear history required on main | `true` |

## Execution envelope

Mutations outside this envelope are forbidden (PLAN-SCHEMA-004).

### Filesystem

- **write_allow:** `l9-ci-sdk: l9_ci/rulesets/semgrep/python/*.yml, tests/fixtures/semgrep/rules/python/, docs/adr/`, `l9-ci-core: semgrep-policy.yaml, .l9/sdk-compatibility.yaml and sdk-pin-mirrors paths, provision-sdk locks, tests/provisioning/, org-ci.yml, analyze-semgrep.yml, AGENTS.md (append-only)`, `.github/workflows/`, `.github/dependabot.yml`, `.pre-commit-config.yaml`, `pyproject.toml`, `requirements-ci.txt`, `requirements-ci.lock`, `requirements.lock`, `tools/lock_requirements.sh`, `tools/deps/exclude-newer.txt`, `scripts/install_locked.sh`, `tests/unit/test_requirement_locks.py`, `.semgrep/`, `.semgrepignore`, `docs/CI_PIPELINE.md`, `Current Work/ (deletion only)`
- **write_deny:** `app/`, `migrations/`, `Makefile`, `Dockerfile`, `Dockerfile.prod`, `docker-compose*.yml`, `tests/ (except tests/unit/test_requirement_locks.py)`
- **delete_allow:** `.github/workflows/docker-build.yml`, `.github/workflows/ci.yml`, `.github/workflows/baseline-ratchet-caller.yml`, `Current Work/`

### Commands

- **allow:** `.pre-commit-config.yaml`, `make lint`, `make test`, `bash tools/lock_requirements.sh --check`, `semgrep --test --config .semgrep/semgrep-rules.yaml .semgrep/fixtures/semgrep-rules.py`, `python3 /root/.cursor-governance/ops/ci_parity/run.py --gate HEAD --lanes semgrep-l9`, `cd l9-ci-sdk && ruff check . && ruff format --check . && mypy l9_ci && pytest -q && semgrep --test --config l9_ci/rulesets/semgrep/python tests/fixtures/semgrep/rules/python`, `python3 /root/.cursor-governance/ops/ci_parity/run.py --gate HEAD --workspace <eie> --base <sha> --lanes semgrep-l9 (with promoted SDK)`, `cd l9-ci-core && python3 tools/advance_core_action_pins.py <sha> --check && pytest -q tests/provisioning`, `gh api repos/Quantum-L9/Enrichment.Inference.Engine/commits/<pr-head>/check-runs (Analyze (central Core))`, `gh api repos/Quantum-L9/Enrichment.Inference.Engine/commits/<main-sha>/check-runs (Vulnerability Scan, OpenSSF Scorecard)`, `toolchain-alignment step output in Analyze (central Core) on EIE`, `bash tools/lock_requirements.sh --check && pytest -q tests/unit/test_requirement_locks.py && ! grep -rnE 'pip install (-r requirements\|.*git\+)' .github/workflows`, `pytest -q (EIE full suite as CI runs it)`, `semgrep --test --config .semgrep/semgrep-rules.yaml .semgrep/fixtures/semgrep-rules.py && semgrep --config .semgrep --error .`, `grep -hoE 'uses: [^ ]+@[^ ]+' .github/workflows/*.yml \| grep -vcE '@[0-9a-f]{40}'`, `SonarCloud api/issues/search rules=githubactions:S8541,githubactions:S8544 on main`, `pre-commit run --config .pre-commit-config.yaml --from-ref origin/main --to-ref HEAD (each EIE PR)`
- **deny:** `git push --force`, `git reset --hard`, admin merge, committing secrets

### Network

| Field | Value |
|-------|-------|
| mode | `named_services_only` |
| allowed_services | `github.com`, `api.github.com`, `pypi.org`, `files.pythonhosted.org`, `sonarcloud.io` |

### Secrets

| Field | Value |
|-------|-------|
| access | `none` |
| redaction_required | `true` |

### Autonomous merge

`autonomous_merge:` `false` always. This plan confers no merge authority.

### Preserved contracts

- CI must never fail on a false positive; precision at the rule owner, never suppression or code rewording
- never skip, xfail or weaken a test to go green
- publication only via make pr; merges via stack_safe_merge.py (squash; unstacked)
- Docker/deploy files need explicit APPROVED (rules 90/93)
- C-18 L9_META headers on new tracked files where the repo requires them
- SDK rule ids and canonical_rule_ids unchanged

## Side effects and idempotency

Required for every destructive / external-write TODO (PLAN-SCHEMA-005). One row per todo.

| todo_id | side_effects | idempotency | retry | compensation | irreversible |
|---------|--------------|-------------|-------|--------------|--------------|
| T1 | `filesystem_mutation` | `safe_with_dedupe` | `retry_once` | `git restore -- l9-ci-sdk:l9_ci/rulesets/semgrep/python/l9-logging.yml l9-ci-sdk:l9_ci/rulesets/semgrep/python/l9-baseline-security.yml l9-ci-sdk:l9_ci/rulesets/semgrep/python/l9-routing.yml l9-ci-sdk:l9_ci/rulesets/semgrep/python/l9-transport.yml l9-ci-sdk:l9_ci/rulesets/semgrep/python/l9-handler-signature.yml l9-ci-sdk:tests/fixtures/semgrep/rules/python/ l9-ci-sdk:docs/adr/0019-rule-precision-saas-and-usage-counts.md` | `false` |
| T2 | `filesystem_mutation` | `safe_with_dedupe` | `retry_once` | `git restore -- l9-ci-core:.github/actions/resolve-governance/semgrep-policy.yaml` | `false` |
| T3 | `filesystem_mutation` | `safe_with_dedupe` | `retry_once` | `git restore -- l9-ci-core:.l9/sdk-compatibility.yaml l9-ci-core:.github/actions/provision-sdk/action.yml l9-ci-core:.github/actions/provision-sdk/provision.py l9-ci-core:.github/actions/provision-sdk/lock_runtime.py l9-ci-core:.github/actions/provision-sdk/locks/ l9-ci-core:.github/workflows/normalize-semgrep-report.yml l9-ci-core:.github/workflows/publish-analysis.yml l9-ci-core:.github/workflows/sdk-contract-check.yml l9-ci-core:.l9/architecture.yaml l9-ci-core:.l9/artifact-protocol.yaml l9-ci-core:tests/provisioning/` | `false` |
| T4 | `filesystem_mutation` | `safe_with_dedupe` | `retry_once` | `git restore -- l9-ci-core:.github/workflows/org-ci.yml l9-ci-core:.github/workflows/analyze-semgrep.yml l9-ci-core:AGENTS.md` | `false` |
| T5 | `destructive_filesystem_mutation` | `unsafe_blind_repeat` | `manual_only` | `git restore -- .github/workflows/docker-build.yml .github/workflows/ci.yml .github/workflows/baseline-ratchet-caller.yml Current Work/` | `false` |
| T6 | `filesystem_mutation` | `safe_with_dedupe` | `retry_once` | `git restore -- pyproject.toml requirements-ci.txt .pre-commit-config.yaml .github/dependabot.yml` | `false` |
| T7 | `filesystem_mutation` | `safe_with_dedupe` | `retry_once` | `git restore -- .github/workflows/ci.yml docs/CI_PIPELINE.md` | `false` |
| T8 | `filesystem_mutation` | `safe_with_dedupe` | `retry_once` | `git restore -- tools/lock_requirements.sh tools/deps/exclude-newer.txt requirements-ci.lock requirements.lock scripts/install_locked.sh .github/workflows/ tests/unit/test_requirement_locks.py` | `false` |
| T9 | `filesystem_mutation` | `safe_with_dedupe` | `retry_once` | `git restore -- .semgrep/semgrep-rules.yaml .semgrep/fixtures/semgrep-rules.py .semgrepignore .github/workflows/` | `false` |
| T10 | `filesystem_mutation` | `safe_with_dedupe` | `retry_once` | `git restore -- .github/workflows/` | `false` |
| T11 | `filesystem_mutation` | `safe_with_dedupe` | `retry_once` | `git restore -- .github/workflows/docker-build.yml` | `false` |
| T12 | `filesystem_mutation` | `safe_with_dedupe` | `retry_once` | `git restore -- blocker: ledger only, no repository file` | `false` |

## Architecture impact

| todo_id | bounded_context | layer | owning_contract | prohibited |
|---------|-----------------|-------|-----------------|------------|
| T1 | `repository root` | `ops` | `CI must never fail on a false positive; precision at the rule owner, never suppression or code rewording`, `never skip, xfail or weaken a test to go green`, `publication only via make pr; merges via stack_safe_merge.py (squash; unstacked)`, `Docker/deploy files need explicit APPROVED (rules 90/93)`, `C-18 L9_META headers on new tracked files where the repo requires them`, `SDK rule ids and canonical_rule_ids unchanged` | `app/`, `migrations/`, `Makefile`, `Dockerfile`, `Dockerfile.prod`, `docker-compose*.yml`, `tests/ (except tests/unit/test_requirement_locks.py)` |
| T2 | `l9-ci-core:.github/actions/resolve-governance` | `ops` | `CI must never fail on a false positive; precision at the rule owner, never suppression or code rewording`, `never skip, xfail or weaken a test to go green`, `publication only via make pr; merges via stack_safe_merge.py (squash; unstacked)`, `Docker/deploy files need explicit APPROVED (rules 90/93)`, `C-18 L9_META headers on new tracked files where the repo requires them`, `SDK rule ids and canonical_rule_ids unchanged` | `app/`, `migrations/`, `Makefile`, `Dockerfile`, `Dockerfile.prod`, `docker-compose*.yml`, `tests/ (except tests/unit/test_requirement_locks.py)` |
| T3 | `repository root` | `ops` | `CI must never fail on a false positive; precision at the rule owner, never suppression or code rewording`, `never skip, xfail or weaken a test to go green`, `publication only via make pr; merges via stack_safe_merge.py (squash; unstacked)`, `Docker/deploy files need explicit APPROVED (rules 90/93)`, `C-18 L9_META headers on new tracked files where the repo requires them`, `SDK rule ids and canonical_rule_ids unchanged` | `app/`, `migrations/`, `Makefile`, `Dockerfile`, `Dockerfile.prod`, `docker-compose*.yml`, `tests/ (except tests/unit/test_requirement_locks.py)` |
| T4 | `repository root` | `ops` | `CI must never fail on a false positive; precision at the rule owner, never suppression or code rewording`, `never skip, xfail or weaken a test to go green`, `publication only via make pr; merges via stack_safe_merge.py (squash; unstacked)`, `Docker/deploy files need explicit APPROVED (rules 90/93)`, `C-18 L9_META headers on new tracked files where the repo requires them`, `SDK rule ids and canonical_rule_ids unchanged` | `app/`, `migrations/`, `Makefile`, `Dockerfile`, `Dockerfile.prod`, `docker-compose*.yml`, `tests/ (except tests/unit/test_requirement_locks.py)` |
| T5 | `repository root` | `ops` | `CI must never fail on a false positive; precision at the rule owner, never suppression or code rewording`, `never skip, xfail or weaken a test to go green`, `publication only via make pr; merges via stack_safe_merge.py (squash; unstacked)`, `Docker/deploy files need explicit APPROVED (rules 90/93)`, `C-18 L9_META headers on new tracked files where the repo requires them`, `SDK rule ids and canonical_rule_ids unchanged` | `app/`, `migrations/`, `Makefile`, `Dockerfile`, `Dockerfile.prod`, `docker-compose*.yml`, `tests/ (except tests/unit/test_requirement_locks.py)` |
| T6 | `repository root` | `ops` | `CI must never fail on a false positive; precision at the rule owner, never suppression or code rewording`, `never skip, xfail or weaken a test to go green`, `publication only via make pr; merges via stack_safe_merge.py (squash; unstacked)`, `Docker/deploy files need explicit APPROVED (rules 90/93)`, `C-18 L9_META headers on new tracked files where the repo requires them`, `SDK rule ids and canonical_rule_ids unchanged` | `app/`, `migrations/`, `Makefile`, `Dockerfile`, `Dockerfile.prod`, `docker-compose*.yml`, `tests/ (except tests/unit/test_requirement_locks.py)` |
| T7 | `repository root` | `ops` | `CI must never fail on a false positive; precision at the rule owner, never suppression or code rewording`, `never skip, xfail or weaken a test to go green`, `publication only via make pr; merges via stack_safe_merge.py (squash; unstacked)`, `Docker/deploy files need explicit APPROVED (rules 90/93)`, `C-18 L9_META headers on new tracked files where the repo requires them`, `SDK rule ids and canonical_rule_ids unchanged` | `app/`, `migrations/`, `Makefile`, `Dockerfile`, `Dockerfile.prod`, `docker-compose*.yml`, `tests/ (except tests/unit/test_requirement_locks.py)` |
| T8 | `repository root` | `ops` | `CI must never fail on a false positive; precision at the rule owner, never suppression or code rewording`, `never skip, xfail or weaken a test to go green`, `publication only via make pr; merges via stack_safe_merge.py (squash; unstacked)`, `Docker/deploy files need explicit APPROVED (rules 90/93)`, `C-18 L9_META headers on new tracked files where the repo requires them`, `SDK rule ids and canonical_rule_ids unchanged` | `app/`, `migrations/`, `Makefile`, `Dockerfile`, `Dockerfile.prod`, `docker-compose*.yml`, `tests/ (except tests/unit/test_requirement_locks.py)` |
| T9 | `repository root` | `ops` | `CI must never fail on a false positive; precision at the rule owner, never suppression or code rewording`, `never skip, xfail or weaken a test to go green`, `publication only via make pr; merges via stack_safe_merge.py (squash; unstacked)`, `Docker/deploy files need explicit APPROVED (rules 90/93)`, `C-18 L9_META headers on new tracked files where the repo requires them`, `SDK rule ids and canonical_rule_ids unchanged` | `app/`, `migrations/`, `Makefile`, `Dockerfile`, `Dockerfile.prod`, `docker-compose*.yml`, `tests/ (except tests/unit/test_requirement_locks.py)` |
| T10 | `.github` | `ops` | `CI must never fail on a false positive; precision at the rule owner, never suppression or code rewording`, `never skip, xfail or weaken a test to go green`, `publication only via make pr; merges via stack_safe_merge.py (squash; unstacked)`, `Docker/deploy files need explicit APPROVED (rules 90/93)`, `C-18 L9_META headers on new tracked files where the repo requires them`, `SDK rule ids and canonical_rule_ids unchanged` | `app/`, `migrations/`, `Makefile`, `Dockerfile`, `Dockerfile.prod`, `docker-compose*.yml`, `tests/ (except tests/unit/test_requirement_locks.py)` |
| T11 | `.github/workflows` | `ops` | `CI must never fail on a false positive; precision at the rule owner, never suppression or code rewording`, `never skip, xfail or weaken a test to go green`, `publication only via make pr; merges via stack_safe_merge.py (squash; unstacked)`, `Docker/deploy files need explicit APPROVED (rules 90/93)`, `C-18 L9_META headers on new tracked files where the repo requires them`, `SDK rule ids and canonical_rule_ids unchanged` | `app/`, `migrations/`, `Makefile`, `Dockerfile`, `Dockerfile.prod`, `docker-compose*.yml`, `tests/ (except tests/unit/test_requirement_locks.py)` |
| T12 | `repository root` | `ops` | `CI must never fail on a false positive; precision at the rule owner, never suppression or code rewording`, `never skip, xfail or weaken a test to go green`, `publication only via make pr; merges via stack_safe_merge.py (squash; unstacked)`, `Docker/deploy files need explicit APPROVED (rules 90/93)`, `C-18 L9_META headers on new tracked files where the repo requires them`, `SDK rule ids and canonical_rule_ids unchanged` | `app/`, `migrations/`, `Makefile`, `Dockerfile`, `Dockerfile.prod`, `docker-compose*.yml`, `tests/ (except tests/unit/test_requirement_locks.py)` |

## Rollback

`schema_ref:` `canonical.schema.rollback_contract.v1`
`instance_binding:` `rollback_contract_ref` → `rollback.plan.simple.eie_ci_and_compliance_catch_up_to_ceg_parity.v1`

| Field | Value |
|-------|-------|
| rollback_id | `rollback.plan.simple.eie_ci_and_compliance_catch_up_to_ceg_parity.v1` |
| source_execution_ref | `plan.simple.eie_ci_and_compliance_catch_up_to_ceg_parity.v1` |
| supported | `true` |
| automatic_allowed | `false` |
| approval_required | `true` |
| trigger_conditions | C1 fails (V1 plus ci-parity with the merged SDK ruleset on EIE: blocking 0 apart from insecure-file-permissions (advisory under v4)) → if any true-positive fixture line stops matching, or another repo gains a finding, fix the rule; never accept a recall loss to clear EIE; C2 fails (V3 and V4) → if Core provisioning tests fail, do not advance step 2; repair step 1 at its owner; C3 fails (V5 and V6 on the first main push) → if Vulnerability Scan still fails, read the job log before PR-3; do not stack fixes on an unproven one; C4 fails (V7, V8, V9, V11) → if --check flaps without a repo change, the seed or cutoff is wrong; fix the renderer, never drop the gate; C5 fails (V10; ledger entries present in session_debt status) → report the remaining items as deferred with reasons |

### Strategies (typed — PLAN-SCHEMA-009)

| domain | mode | notes |
|--------|------|-------|
| code | `git_restore_scoped_paths` | SDK: revert PR, Core keeps 6d70e829 as rollback-compatible revision; Core: revert step 2 then step 1; EIE PRs: each independently revertible by squash revert; PR-7: revert restores DEV-image publication |
| data | `none` | PLAN_DOCUMENT declares no data mutation |
| external_state | `none` | PLAN_DOCUMENT declares no external write |
| local_state | `git_restore_scoped_paths` | scoped to todo files |

### Irreversible operations

- none — no todo is marked `irreversible` (PLAN-SCHEMA-010)

### Rollback verification

- re-run `.pre-commit-config.yaml`
- re-run `make lint`
- re-run `make test`
- re-run `bash tools/lock_requirements.sh --check`
- re-run `semgrep --test --config .semgrep/semgrep-rules.yaml .semgrep/fixtures/semgrep-rules.py`
- re-run `python3 /root/.cursor-governance/ops/ci_parity/run.py --gate HEAD --lanes semgrep-l9`
- re-run `cd l9-ci-sdk && ruff check . && ruff format --check . && mypy l9_ci && pytest -q && semgrep --test --config l9_ci/rulesets/semgrep/python tests/fixtures/semgrep/rules/python`
- re-run `python3 /root/.cursor-governance/ops/ci_parity/run.py --gate HEAD --workspace <eie> --base <sha> --lanes semgrep-l9 (with promoted SDK)`
- re-run `cd l9-ci-core && python3 tools/advance_core_action_pins.py <sha> --check && pytest -q tests/provisioning`
- re-run `gh api repos/Quantum-L9/Enrichment.Inference.Engine/commits/<pr-head>/check-runs (Analyze (central Core))`
- re-run `gh api repos/Quantum-L9/Enrichment.Inference.Engine/commits/<main-sha>/check-runs (Vulnerability Scan, OpenSSF Scorecard)`
- re-run `toolchain-alignment step output in Analyze (central Core) on EIE`
- re-run `bash tools/lock_requirements.sh --check && pytest -q tests/unit/test_requirement_locks.py && ! grep -rnE 'pip install (-r requirements|.*git\+)' .github/workflows`
- re-run `pytest -q (EIE full suite as CI runs it)`
- re-run `semgrep --test --config .semgrep/semgrep-rules.yaml .semgrep/fixtures/semgrep-rules.py && semgrep --config .semgrep --error .`
- re-run `grep -hoE 'uses: [^ ]+@[^ ]+' .github/workflows/*.yml | grep -vcE '@[0-9a-f]{40}'`
- re-run `SonarCloud api/issues/search rules=githubactions:S8541,githubactions:S8544 on main`
- re-run `pre-commit run --config .pre-commit-config.yaml --from-ref origin/main --to-ref HEAD (each EIE PR)`

## Complexity and uncertainty

| Field | Value |
|-------|-------|
| complexity | `high` — highest todo risk across 12 todo(s) |
| uncertainty | `low` — 0 unknown(s), 0 blocking |
| blast_radius | `high` — An SDK recall regression would silently stop blocking real secret logging or Gate bypass across every governed repo. A bad Core promotion reds every repo's required check. A wrong lock render breaks every EIE CI job. Dockerfile.prod publication changes the running production image. |
| architectural_boundaries_crossed | `org CI policy (SDK rulesets, Core semgrep-policy)`, `Core SDK provisioning pin`, `dependency supply chain (hash locks)`, `image publication (gated)` |
| external_systems_touched | `GitHub Actions`, `PyPI`, `GHCR`, `SonarCloud` |
| migration_required | `false` |
| unknown_dependency_count | `0` |

## Inventory and classification *(optional — activate if retire/migrate/replace)*

Not activated — PLAN_DOCUMENT declares no retire / migrate / replace inventory.

## Gated write pipeline *(optional — irreversible or external writes)*

Not activated — PLAN_DOCUMENT declares no irreversible or external write.

## Regeneration extinguishment *(optional — retirement/deprecation)*

Not activated — PLAN_DOCUMENT declares no retirement or deprecation.

## Execution DAG

`schema_ref:` `canonical.schema.dependency_topology.v1`
`instance_binding:` `dependency_topology_ref` → `dag.plan.simple.eie_ci_and_compliance_catch_up_to_ceg_parity.v1`
Must be acyclic before status may become `executable` (PLAN-SCHEMA-007).

| Field | Value |
|-------|-------|
| topology_id | `dag.plan.simple.eie_ci_and_compliance_catch_up_to_ceg_parity.v1` |
| topology_kind | `execution` |
| graph_type | `directed_acyclic_graph` |

### Nodes / edges

| id | task | owner | risk | effort | depends_on | outputs |
|----|------|-------|------|--------|------------|---------|
| T1 | l9-ci-sdk PR: precision for the token regex (exclude count/tokenize forms, keep credential *_tokens), constant-only sql-string-format (flag only non-UPPER_CASE interpolations), receiver-scoped routing and raw-execute rules (L9 node/peer/gate sources only), and a handler-name requirement for raw-payload-parameter. Keep canonical_rule_ids, rule ids and L9_META headers; add TP/TN fixture lines and ADR-0019 | Cursor Build agent | `medium` | M | `[]` | `l9-ci-sdk:l9_ci/rulesets/semgrep/python/l9-logging.yml`, `l9-ci-sdk:l9_ci/rulesets/semgrep/python/l9-baseline-security.yml`, `l9-ci-sdk:l9_ci/rulesets/semgrep/python/l9-routing.yml`, `l9-ci-sdk:l9_ci/rulesets/semgrep/python/l9-transport.yml`, `l9-ci-sdk:l9_ci/rulesets/semgrep/python/l9-handler-signature.yml`, `l9-ci-sdk:tests/fixtures/semgrep/rules/python/`, `l9-ci-sdk:docs/adr/0019-rule-precision-saas-and-usage-counts.md` |
| T2 | l9-ci-core PR: semgrep-policy.yaml metadata.version 4, classify insecure-file-permissions as advisory and extend the note with the numeric-mode reason (0o700 owner-only reported as permissive) | Cursor Build agent | `low` | S | `[]` | `l9-ci-core:.github/actions/resolve-governance/semgrep-policy.yaml` |
| T3 | l9-ci-core step 1: promote the T1 merge SHA in .l9/sdk-compatibility.yaml (keep 6d70e829 as rollback), update every sdk-pin-mirrors path, regenerate the provision-sdk runtime lock with lock_runtime.py, update tests/provisioning | Cursor Build agent | `medium` | M | `[T1]` | `l9-ci-core:.l9/sdk-compatibility.yaml`, `l9-ci-core:.github/actions/provision-sdk/action.yml`, `l9-ci-core:.github/actions/provision-sdk/provision.py`, `l9-ci-core:.github/actions/provision-sdk/lock_runtime.py`, `l9-ci-core:.github/actions/provision-sdk/locks/`, `l9-ci-core:.github/workflows/normalize-semgrep-report.yml`, `l9-ci-core:.github/workflows/publish-analysis.yml`, `l9-ci-core:.github/workflows/sdk-contract-check.yml`, `l9-ci-core:.l9/architecture.yaml`, `l9-ci-core:.l9/artifact-protocol.yaml`, `l9-ci-core:tests/provisioning/` |
| T4 | l9-ci-core step 2: advance org-ci.yml Core action pins to the step-1 commit and its sdk-revision fallback to the T1 SHA; prove with tools/advance_core_action_pins.py <sha> --check; add the dated AGENTS.md activation section (append-only) | Cursor Build agent | `medium` | S | `[T2, T3]` | `l9-ci-core:.github/workflows/org-ci.yml`, `l9-ci-core:.github/workflows/analyze-semgrep.yml`, `l9-ci-core:AGENTS.md` |
| T5 | EIE PR-1: docker-build.yml add `actions: read`; ci.yml scorecard job add issues/pull-requests/checks read (CEG #416); delete .github/workflows/baseline-ratchet-caller.yml (ADR-0016); git rm the 16 tracked `Current Work/` files (already in .gitignore) | Cursor Build agent | `low` | S | `[]` | `.github/workflows/docker-build.yml`, `.github/workflows/ci.yml`, `.github/workflows/baseline-ratchet-caller.yml`, `Current Work/` |
| T6 | EIE PR-2: ruff 0.16.1 / mypy 2.3.0 / pytest 9.1.1 in pyproject [dev], requirements-ci.txt and pre-commit; pre-commit revs by SHA with tag comments; mirrors-mypy additional_dependencies equal to the CI type-check deps; pytest-unit hook drops `2>/dev/null \|\| true`; Dependabot pip ignore for ruff/mypy/pytest (Core-owned); requires-python >=3.12 (CEG #409) | Cursor Build agent | `low` | S | `[]` | `pyproject.toml`, `requirements-ci.txt`, `.pre-commit-config.yaml`, `.github/dependabot.yml` |
| T7 | EIE PR-3: split the networked `validate_sdk_pin.py --verify-tag` step out of validate into an sdk-channel job that CI Gate reports but validate no longer waits on (CEG #412); keep the offline pin check in validate | Cursor Build agent | `low` | S | `[T5]` | `.github/workflows/ci.yml`, `docs/CI_PIPELINE.md` |
| T8 | EIE PR-4: tools/lock_requirements.sh renders seeded from the committed lock with a recorded --exclude-newer cutoff (tools/deps/exclude-newer.txt) and pinned uv; add --check (exit 1 on drift) plus a requirements-ci.lock hashed render; add scripts/install_locked.sh (--require-hashes) and switch all 13 requirements installs plus the pr-pipeline git+@v1 SDK install; add a lock --check step to sdk-channel; add a unit test proving --check fails on a stale lock (CEG #394/#419) | Cursor Build agent | `medium` | M | `[T6, T7]` | `tools/lock_requirements.sh`, `tools/deps/exclude-newer.txt`, `requirements-ci.lock`, `requirements.lock`, `scripts/install_locked.sh`, `.github/workflows/`, `tests/unit/test_requirement_locks.py` |
| T9 | EIE PR-5: pin the CI Semgrep install; port CEG #418 precise float/singleton rules into .semgrep/semgrep-rules.yaml with fixtures under .semgrep/fixtures/, a .semgrepignore and a semgrep --test CI step | Cursor Build agent | `low` | S | `[T8]` | `.semgrep/semgrep-rules.yaml`, `.semgrep/fixtures/semgrep-rules.py`, `.semgrepignore`, `.github/workflows/` |
| T10 | EIE PR-6: SHA-pin all 96 tag-pinned workflow `uses:` refs with `# vX` comments; the Dependabot github-actions ecosystem keeps them current | Cursor Build agent | `low` | S | `[T7, T8, T9]` | `.github/workflows/` |
| T11 | EIE PR-7, BLOCKED until explicit operator APPROVED (rules 90/93, Docker/deploy protection): docker-build.yml build-push-action `file: Dockerfile.prod`, so CI publishes the digest-pinned, hash-locked, non-root production image instead of the DEV image | Cursor Build agent | `high` | S | `[T5, T8]` | `.github/workflows/docker-build.yml` |
| T12 | Ledger: record session_debt findings for the CEG follow-ups (docker-build actions: read; dead baseline-ratchet caller), EIE V2 contract adoption, SonarCloud residual (Dockerfile + text:S8565) after PR-4, and Dockerfile curl/git runtime removal pending APPROVED | Cursor Build agent | `low` | S | `[T8]` | `blocker: ledger only, no repository file` |

**Critical path:** T1 → T3 → T4 → T5 → T7 → T8 → T10

**Forbidden edges:** any edge not listed above; any todo writing outside `write_allow`.

### Milestones

| id | outcome | unlocks |
|----|---------|---------|
| M1 | T1 and T2 merged (SDK precision, Core policy v4) | T3 promotion to an exact merged SDK SHA |
| M2 | T3 and T4 merged: the required check evaluates with the precise SDK | EIE PR merges (Analyze (central Core) green) |
| M3 | PR-1 and PR-2 merged: main red checks cleared, toolchain aligned | PR-3 / PR-4 on fresh main |
| M4 | PR-3, PR-4 and PR-5 merged: sdk-channel, locks and precise local rules | PR-6 SHA pinning over the final workflow set |
| M5 | PR-6 merged; PR-7 merged or held on the operator decision; ledger written | convergence |

### Checkpoints

| id | after | evidence_required | no_go_action |
|----|-------|-------------------|--------------|
| C1 | M1 | V1 plus ci-parity with the merged SDK ruleset on EIE: blocking 0 apart from insecure-file-permissions (advisory under v4) | if any true-positive fixture line stops matching, or another repo gains a finding, fix the rule; never accept a recall loss to clear EIE |
| C2 | M2 | V3 and V4 | if Core provisioning tests fail, do not advance step 2; repair step 1 at its owner |
| C3 | M3 | V5 and V6 on the first main push | if Vulnerability Scan still fails, read the job log before PR-3; do not stack fixes on an unproven one |
| C4 | M4 | V7, V8, V9, V11 | if --check flaps without a repo change, the seed or cutoff is wrong; fix the renderer, never drop the gate |
| C5 | M5 | V10; ledger entries present in session_debt status | report the remaining items as deferred with reasons |

### Leverage

- **Ranked todos:** `T1`, `T3`, `T4`, `T2`, `T5`, `T6`, `T8`, `T7`, `T9`, `T10`, `T11`, `T12`
- **Shared causes:** verdicts set by an owner other than the property they claim (rule over-approximation, local re-declaration of Core-owned versions, index-dependent lock render); the dead or degraded instruments (dead caller, || true hook, missing permissions) report red or green without meaning
- **Deletions / consolidations:** delete baseline-ratchet-caller.yml; delete 16 tracked Current Work/ files; delete EIE-local tool version drift (3 declarations collapse to Core pins)

### Doc / root surface impact

| surface | action | todo_ids | reason |
|---------|--------|----------|--------|
| AGENTS.md (EIE) | `n_a` | — | no command, contract or tier change; Makefile targets unchanged |
| docs/CI_PIPELINE.md (EIE) | `update` | `T7`, `T8`, `T9` | — |
| l9-ci-sdk docs/adr | `update` | `T1` | — |
| l9-ci-core AGENTS.md dated activation section | `update` | `T4` | — |
| Makefile (EIE) | `n_a` | — | append-only root file; no target change needed |

## Property evidence matrix

`schema_ref:` `canonical.schema.validation_evidence.v1`
`instance_binding:` `validation_evidence_refs` / `property_evidence_matrix_ref`
Exit-0 alone is insufficient when property needs structural/runtime proof (PLAN-SCHEMA-008).

| evidence_id | claim_id / SP | evidence_kind | method | command | expected_positive | status |
|-------------|---------------|---------------|--------|---------|-------------------|--------|
| E-V1 | SP-01 | `quality_gate_evidence` | command | `cd l9-ci-sdk && ruff check . && ruff format --check . && mypy l9_ci && pytest -q && semgrep --test --config l9_ci/rulesets/semgrep/python tests/fixtures/semgrep/rules/python` | all pass, including new TP/TN lines | `not_run` |
| E-V2 | SP-02 | `quality_gate_evidence` | command | `python3 /root/.cursor-governance/ops/ci_parity/run.py --gate HEAD --workspace <eie> --base <sha> --lanes semgrep-l9 (with promoted SDK)` | blocking=0 | `not_run` |
| E-V3 | SP-03, SP-04 | `quality_gate_evidence` | command | `cd l9-ci-core && python3 tools/advance_core_action_pins.py <sha> --check && pytest -q tests/provisioning` | pins proven; provisioning tests pass; Core self-CI green | `not_run` |
| E-V4 | SP-04 | `quality_gate_evidence` | command | `gh api repos/Quantum-L9/Enrichment.Inference.Engine/commits/<pr-head>/check-runs (Analyze (central Core))` | conclusion success | `not_run` |
| E-V5 | SP-05 | `quality_gate_evidence` | command | `gh api repos/Quantum-L9/Enrichment.Inference.Engine/commits/<main-sha>/check-runs (Vulnerability Scan, OpenSSF Scorecard)` | both success; no baseline-ratchet run | `not_run` |
| E-V6 | SP-06 | `quality_gate_evidence` | command | `toolchain-alignment step output in Analyze (central Core) on EIE` | 0 mismatches | `not_run` |
| E-V7 | SP-07 | `quality_gate_evidence` | command | `bash tools/lock_requirements.sh --check && pytest -q tests/unit/test_requirement_locks.py && ! grep -rnE 'pip install (-r requirements\|.*git\+)' .github/workflows` | --check exit 0 on main; negative-control test passes; no unhashed install remains | `not_run` |
| E-V8 | SP-08 | `quality_gate_evidence` | command | `pytest -q (EIE full suite as CI runs it)` | >=3032 passed, skipped <=25, xfailed <=4, 0 failed | `not_run` |
| E-V9 | SP-09 | `quality_gate_evidence` | command | `semgrep --test --config .semgrep/semgrep-rules.yaml .semgrep/fixtures/semgrep-rules.py && semgrep --config .semgrep --error .` | fixture pass; 0 findings | `not_run` |
| E-V10 | SP-10 | `quality_gate_evidence` | command | `grep -hoE 'uses: [^ ]+@[^ ]+' .github/workflows/*.yml \| grep -vcE '@[0-9a-f]{40}'` | 0 | `not_run` |
| E-V11 | SP-11 | `quality_gate_evidence` | command | `SonarCloud api/issues/search rules=githubactions:S8541,githubactions:S8544 on main` | 0 open | `not_run` |
| E-V12 | SP-12 | `quality_gate_evidence` | command | `pre-commit run --config .pre-commit-config.yaml --from-ref origin/main --to-ref HEAD (each EIE PR)` | all hooks pass, including the now-failing-capable pytest-unit hook | `not_run` |

## Stress and disconfirm

### Disconfirming cases

- Does the narrowed token regex still catch access_token, refresh_tokens and auth token values passed to a logger?
- Could the constant-only SQL rule miss f"... {user_input}" when the variable is UPPER_CASE by convention?
- Do the receiver-scoped routing rules miss an L9 node call made through a generic httpx.post with a URL built from settings?
- Can EIE PRs merge before the Core promotion, given that the required check is red on 25 app/ findings until T4?
- Does publishing Dockerfile.prod break a deploy that relied on the DEV entrypoint's auto-migration?
- Will SHA-pinning 96 refs conflict textually with PR-3/4/5 edits to the same workflow files?

### Assumption failure conditions

- the 27 findings contain a real secret, SQL or node-dispatch defect (audit: none; each read at file:line)
- another governed repo depends on the over-broad rules for a real true positive (prototype: 0 delta across 4 repos)

### Blast radius notes

- An SDK recall regression would silently stop blocking real secret logging or Gate bypass across every governed repo. A bad Core promotion reds every repo's required check. A wrong lock render breaks every EIE CI job. Dockerfile.prod publication changes the running production image.

### Rollback constraints

- SDK: revert PR, Core keeps 6d70e829 as rollback-compatible revision; Core: revert step 2 then step 1; EIE PRs: each independently revertible by squash revert; PR-7: revert restores DEV-image publication

### Risks

| risk | mitigation |
|------|------------|
| SDK precision loses a true positive fleet-wide | TP fixtures for each narrowed rule; cross-repo ci-parity delta at C1; promotion keeps rollback revision |
| EIE PRs sit red on the required check until T4 merges | publish EIE PRs in parallel and converge every other check; merge once V4 holds; this is sequencing, not a wait-for-merge finish |
| linear history rejects stacked merge commits | publish every EIE PR unstacked from origin/main (PR_STACK empty) so stack_safe_merge selects squash; T10 last to avoid overlap |
| networked lock --check flaps on PyPI/GitHub outage | runs only in the networked sdk-channel job; seed and --exclude-newer make the render repository-determined |
| PR-7 changes deployed image behaviour (no auto-migrate, py3.12, non-root) | blocked on explicit operator APPROVED; production schema step is already an operator release step per docker-entrypoint.sh |

## Out of scope

- EIE application code under app/, migrations/ (separate concern per operator)
- V2 execution contract adoption (.l9/repo-workflow.json, facade Makefile, Repo.mk) - later, deliberate
- 4 known-gap xfails and 25 cross-engine e2e skips
- Dockerfile and Dockerfile.prod edits (curl/git removal, python healthcheck) without explicit APPROVED
- CEG follow-ups: docker-build actions: read and the dead baseline-ratchet caller in CEG (ledgered, not executed)
- open EIE PR #282 contents
- SonarCloud residual Dockerfile and text:S8565 issues (re-measured after PR-4, ledgered)
- Architecture redesign (unless plan_class + redesign_allowed)
- Force-push, hard-reset, admin-merge, secret exfil
- Weakening scanners / gates to obtain PASS
- Follow-on schema/platform evolution

## Follow-on milestone *(optional — keep separate; PLAN-SCHEMA-014)*

Not activated — PLAN_DOCUMENT declares no follow-on milestone; separate_plan_required `true` for any follow-on.

## Convergence

`schema_ref:` `canonical.schema.convergence_contract.v1`
`instance_binding:` `convergence_contract_ref` → `conv.plan.simple.eie_ci_and_compliance_catch_up_to_ceg_parity.v1`
Convergence requires all blocking evidence + gates (PLAN-SCHEMA-015).

| Field | Value |
|-------|-------|
| convergence_id | `conv.plan.simple.eie_ci_and_compliance_catch_up_to_ceg_parity.v1` |
| source_ref | `plan.simple.eie_ci_and_compliance_catch_up_to_ceg_parity.v1` |
| current_state | `draft` |
| implementation_ready | `false` |

### Gates

- **executable_when:**
  - baseline locked + reverified
  - blocking capability probes pass
  - DAG acyclic
  - envelope + side-effect matrix complete for mutate todos
  - no blocking unknowns
- **complete_when:**
  - all blocking success-property evidence `passed`
  - rollback contract still valid / unused-or-verified
  - out_of_scope respected (diff hygiene)
- **blocking_conditions:**
  - `preflight_blocked`
  - envelope breach
  - baseline drift
  - failed blocking property

### Evidence

- **required_evidence_refs:** E-V1, E-V2, E-V3, E-V4, E-V5, E-V6, E-V7, E-V8, E-V9, E-V10, E-V11, E-V12
- **observed_evidence_refs:** none
- **missing_evidence:** E-V1, E-V2, E-V3, E-V4, E-V5, E-V6, E-V7, E-V8, E-V9, E-V10, E-V11, E-V12

### Blockers / unknowns

| kind | id | note | resolution |
|------|----|------|------------|
| `open_blocker` | stop_reason | plan validated; U1-U3 resolved by probes; awaiting operator approval before any mutation; T11 additionally needs explicit APPROVED | planning convergence `partial` |

### Next

| Field | Value |
|-------|-------|
| next_convergence_gate | `executable_when` |
| minimum_safe_next_action | When law holds and status=`executable`, press **Build**, stack if any open PR exists (`PR_STACK=auto`), then `make pr` and display the PR URL — do not free-form execute |
| execute_via | Cursor Build; stacked PR if any open PR exists; display PR URL |
| next_skill | Build then stacked make pr |
| broader_work_requires_separate_contract | `true` |

---

## Machine stub (optional YAML instance seed)

Projected from PLAN_DOCUMENT; keep in sync with the sections above.

```yaml
schema_id: "canonical.schema.plan_document.v1"
schema_version: "1.0.0"
metadata:
  plan_id: "plan.simple.eie_ci_and_compliance_catch_up_to_ceg_parity.v1"
  name: "EIE CI and compliance catch-up to CEG parity"
  overview: "Bring Quantum-L9/Enrichment.Inference.Engine main to CEG's CI/compliance level. The required org check Analyze (central Core) must be green with no false-positive-driven verdicts. main's Vulnerability Scan and OpenSSF Scorecard must be green. EIE must hold zero toolchain mismatches against Core, run hash-locked CI installs with a deterministic lock gate, isolate the networked SDK channel check, use precise pinned local Semgrep rules, SHA-pin its workflow actions, and drop dead workflows and scratch. All of this lands through fixes at each instrument's owner (l9-ci-sdk, l9-ci-core, EIE CI files) and never through EIE application-code edits or suppressions."
  status: "draft"
  is_project: false
  owner: "igor_beylin"
architect_framing:
  planning_ssot: "PLAN_DOCUMENT"
  plan_class: "bounded_execution_contract"
  redesign_allowed: false
  follow_on_schema_evolution_separate: true
immutable_baseline:
  repository: "Quantum-L9/Enrichment.Inference.Engine"
  commit_sha: "12d8b351b3c804094d3fe539ea578ea946da494b"
  dirty: "false"
  overlap_policy: "stop_if_dirty_overlaps_may_modify"
  verification_rule: "reverify_at_execution_start"
  on_drift: "stop_and_replan"
objective:
  mission: "Bring Quantum-L9/Enrichment.Inference.Engine main to CEG's CI/compliance level. The required org check Analyze (central Core) must be green with no false-positive-driven verdicts. main's Vulnerability Scan and OpenSSF Scorecard must be green. EIE must hold zero toolchain mismatches against Core, run hash-locked CI installs with a deterministic lock gate, isolate the networked SDK channel check, use precise pinned local Semgrep rules, SHA-pin its workflow actions, and drop dead workflows and scratch. All of this lands through fixes at each instrument's owner (l9-ci-sdk, l9-ci-core, EIE CI files) and never through EIE application-code edits or suppressions."
  success_properties:
    - id: "SP-01"
      property: "V1: l9-ci-sdk precision PR merged; SDK self-CI green; semgrep --test over tests/fixtures/semgrep/rules/python passes, including the new true-positive and true-negative lines"
      evidence_type: "quality_gate"
      proof: ["V1"]
      blocking: true
    - id: "SP-02"
      property: "V2: ci-parity semgrep-l9 lane on EIE main with the promoted SDK reports blocking=0 (baseline 27 at 12d8b351)"
      evidence_type: "quality_gate"
      proof: ["V2"]
      blocking: true
    - id: "SP-03"
      property: "V3: Core step 1 and step 2 merged; `python3 tools/advance_core_action_pins.py <sha> --check` passes; Core self-CI green"
      evidence_type: "quality_gate"
      proof: ["V3"]
      blocking: true
    - id: "SP-04"
      property: "V4: Analyze (central Core) success on an EIE PR head after V3"
      evidence_type: "quality_gate"
      proof: ["V3", "V4"]
      blocking: true
    - id: "SP-05"
      property: "V5: first EIE main push after PR-1 shows Vulnerability Scan and OpenSSF Scorecard success; baseline-ratchet-caller.yml no longer runs"
      evidence_type: "quality_gate"
      proof: ["V5"]
      blocking: true
    - id: "SP-06"
      property: "V6: central toolchain-alignment step on EIE reports 0 mismatches (baseline 8)"
      evidence_type: "quality_gate"
      proof: ["V6"]
      blocking: true
    - id: "SP-07"
      property: "V7: lock --check exits 0 on main and 1 on a stale-lock negative-control test; every CI Python install uses --require-hashes"
      evidence_type: "quality_gate"
      proof: ["V7"]
      blocking: true
    - id: "SP-08"
      property: "V8: EIE pytest suite passes with no new skip or xfail (baseline 3032 passed, 25 skipped, 4 xfailed)"
      evidence_type: "quality_gate"
      proof: ["V8"]
      blocking: true
    - id: "SP-09"
      property: "V9: pinned local semgrep --test passes on the EIE fixture; semgrep --config .semgrep --error . reports 0"
      evidence_type: "quality_gate"
      proof: ["V9"]
      blocking: true
    - id: "SP-10"
      property: "V10: 0 workflow `uses:` refs pinned by mutable tag (baseline 96)"
      evidence_type: "quality_gate"
      proof: ["V10"]
      blocking: true
    - id: "SP-11"
      property: "V11: SonarCloud new-code S8541/S8544 count 0 after PR-4 (baseline ~95)"
      evidence_type: "quality_gate"
      proof: ["V11"]
      blocking: true
    - id: "SP-12"
      property: "V12: every EIE PR passes the .pre-commit-config.yaml hook catalog on its diff"
      evidence_type: "quality_gate"
      proof: ["V12"]
      blocking: true
capability_preflight_ref: "preflight.plan.simple.eie_ci_and_compliance_catch_up_to_ceg_parity.v1"
execution_envelope:
  filesystem:
    write_allow: ["l9-ci-sdk: l9_ci/rulesets/semgrep/python/*.yml, tests/fixtures/semgrep/rules/python/, docs/adr/", "l9-ci-core: semgrep-policy.yaml, .l9/sdk-compatibility.yaml and sdk-pin-mirrors paths, provision-sdk locks, tests/provisioning/, org-ci.yml, analyze-semgrep.yml, AGENTS.md (append-only)", ".github/workflows/", ".github/dependabot.yml", ".pre-commit-config.yaml", "pyproject.toml", "requirements-ci.txt", "requirements-ci.lock", "requirements.lock", "tools/lock_requirements.sh", "tools/deps/exclude-newer.txt", "scripts/install_locked.sh", "tests/unit/test_requirement_locks.py", ".semgrep/", ".semgrepignore", "docs/CI_PIPELINE.md", "Current Work/ (deletion only)"]
    write_deny: ["app/", "migrations/", "Makefile", "Dockerfile", "Dockerfile.prod", "docker-compose*.yml", "tests/ (except tests/unit/test_requirement_locks.py)"]
  commands:
    allow: [".pre-commit-config.yaml", "make lint", "make test", "bash tools/lock_requirements.sh --check", "semgrep --test --config .semgrep/semgrep-rules.yaml .semgrep/fixtures/semgrep-rules.py", "python3 /root/.cursor-governance/ops/ci_parity/run.py --gate HEAD --lanes semgrep-l9", "cd l9-ci-sdk && ruff check . && ruff format --check . && mypy l9_ci && pytest -q && semgrep --test --config l9_ci/rulesets/semgrep/python tests/fixtures/semgrep/rules/python", "python3 /root/.cursor-governance/ops/ci_parity/run.py --gate HEAD --workspace <eie> --base <sha> --lanes semgrep-l9 (with promoted SDK)", "cd l9-ci-core && python3 tools/advance_core_action_pins.py <sha> --check && pytest -q tests/provisioning", "gh api repos/Quantum-L9/Enrichment.Inference.Engine/commits/<pr-head>/check-runs (Analyze (central Core))", "gh api repos/Quantum-L9/Enrichment.Inference.Engine/commits/<main-sha>/check-runs (Vulnerability Scan, OpenSSF Scorecard)", "toolchain-alignment step output in Analyze (central Core) on EIE", "bash tools/lock_requirements.sh --check && pytest -q tests/unit/test_requirement_locks.py && ! grep -rnE 'pip install (-r requirements|.*git\\+)' .github/workflows", "pytest -q (EIE full suite as CI runs it)", "semgrep --test --config .semgrep/semgrep-rules.yaml .semgrep/fixtures/semgrep-rules.py && semgrep --config .semgrep --error .", "grep -hoE 'uses: [^ ]+@[^ ]+' .github/workflows/*.yml | grep -vcE '@[0-9a-f]{40}'", "SonarCloud api/issues/search rules=githubactions:S8541,githubactions:S8544 on main", "pre-commit run --config .pre-commit-config.yaml --from-ref origin/main --to-ref HEAD (each EIE PR)"]
  network:
    mode: "named_services_only"
  secrets:
    access: "none"
    redaction_required: true
  autonomous_merge: false
side_effects_and_idempotency:
  - todo_id: "T1"
    side_effects: "filesystem_mutation"
    idempotency: "safe_with_dedupe"
    retry: "retry_once"
  - todo_id: "T2"
    side_effects: "filesystem_mutation"
    idempotency: "safe_with_dedupe"
    retry: "retry_once"
  - todo_id: "T3"
    side_effects: "filesystem_mutation"
    idempotency: "safe_with_dedupe"
    retry: "retry_once"
  - todo_id: "T4"
    side_effects: "filesystem_mutation"
    idempotency: "safe_with_dedupe"
    retry: "retry_once"
  - todo_id: "T5"
    side_effects: "destructive_filesystem_mutation"
    idempotency: "unsafe_blind_repeat"
    retry: "manual_only"
  - todo_id: "T6"
    side_effects: "filesystem_mutation"
    idempotency: "safe_with_dedupe"
    retry: "retry_once"
  - todo_id: "T7"
    side_effects: "filesystem_mutation"
    idempotency: "safe_with_dedupe"
    retry: "retry_once"
  - todo_id: "T8"
    side_effects: "filesystem_mutation"
    idempotency: "safe_with_dedupe"
    retry: "retry_once"
  - todo_id: "T9"
    side_effects: "filesystem_mutation"
    idempotency: "safe_with_dedupe"
    retry: "retry_once"
  - todo_id: "T10"
    side_effects: "filesystem_mutation"
    idempotency: "safe_with_dedupe"
    retry: "retry_once"
  - todo_id: "T11"
    side_effects: "filesystem_mutation"
    idempotency: "safe_with_dedupe"
    retry: "retry_once"
  - todo_id: "T12"
    side_effects: "filesystem_mutation"
    idempotency: "safe_with_dedupe"
    retry: "retry_once"
rollback_contract_ref: "rollback.plan.simple.eie_ci_and_compliance_catch_up_to_ceg_parity.v1"
dependency_topology_ref: "dag.plan.simple.eie_ci_and_compliance_catch_up_to_ceg_parity.v1"
validation_evidence_refs: ["E-V1", "E-V2", "E-V3", "E-V4", "E-V5", "E-V6", "E-V7", "E-V8", "E-V9", "E-V10", "E-V11", "E-V12"]
stress_and_disconfirm:
  disconfirming_cases: ["Does the narrowed token regex still catch access_token, refresh_tokens and auth token values passed to a logger?", "Could the constant-only SQL rule miss f\"... {user_input}\" when the variable is UPPER_CASE by convention?", "Do the receiver-scoped routing rules miss an L9 node call made through a generic httpx.post with a URL built from settings?", "Can EIE PRs merge before the Core promotion, given that the required check is red on 25 app/ findings until T4?", "Does publishing Dockerfile.prod break a deploy that relied on the DEV entrypoint's auto-migration?", "Will SHA-pinning 96 refs conflict textually with PR-3/4/5 edits to the same workflow files?"]
  assumption_failure_conditions: ["the 27 findings contain a real secret, SQL or node-dispatch defect (audit: none; each read at file:line)", "another governed repo depends on the over-broad rules for a real true positive (prototype: 0 delta across 4 repos)"]
out_of_scope: ["EIE application code under app/, migrations/ (separate concern per operator)", "V2 execution contract adoption (.l9/repo-workflow.json, facade Makefile, Repo.mk) - later, deliberate", "4 known-gap xfails and 25 cross-engine e2e skips", "Dockerfile and Dockerfile.prod edits (curl/git removal, python healthcheck) without explicit APPROVED", "CEG follow-ups: docker-build actions: read and the dead baseline-ratchet caller in CEG (ledgered, not executed)", "open EIE PR #282 contents", "SonarCloud residual Dockerfile and text:S8565 issues (re-measured after PR-4, ledgered)"]
convergence_contract_ref: "conv.plan.simple.eie_ci_and_compliance_catch_up_to_ceg_parity.v1"
execute_via:
  pipeline: cursor-build
  mention_program: "Cursor Build"
  command_ref: PR_STACK=auto make pr
  authority_order:
    - plan_document
    - cursor_build
todos:
  - id: "T1"
    content: "l9-ci-sdk PR: precision for the token regex (exclude count/tokenize forms, keep credential *_tokens), constant-only sql-string-format (flag only non-UPPER_CASE interpolations), receiver-scoped routing and raw-execute rules (L9 node/peer/gate sources only), and a handler-name requirement for raw-payload-parameter. Keep canonical_rule_ids, rule ids and L9_META headers; add TP/TN fixture lines and ADR-0019"
    status: "pending"
  - id: "T2"
    content: "l9-ci-core PR: semgrep-policy.yaml metadata.version 4, classify insecure-file-permissions as advisory and extend the note with the numeric-mode reason (0o700 owner-only reported as permissive)"
    status: "pending"
  - id: "T3"
    content: "l9-ci-core step 1: promote the T1 merge SHA in .l9/sdk-compatibility.yaml (keep 6d70e829 as rollback), update every sdk-pin-mirrors path, regenerate the provision-sdk runtime lock with lock_runtime.py, update tests/provisioning"
    status: "pending"
  - id: "T4"
    content: "l9-ci-core step 2: advance org-ci.yml Core action pins to the step-1 commit and its sdk-revision fallback to the T1 SHA; prove with tools/advance_core_action_pins.py <sha> --check; add the dated AGENTS.md activation section (append-only)"
    status: "pending"
  - id: "T5"
    content: "EIE PR-1: docker-build.yml add `actions: read`; ci.yml scorecard job add issues/pull-requests/checks read (CEG #416); delete .github/workflows/baseline-ratchet-caller.yml (ADR-0016); git rm the 16 tracked `Current Work/` files (already in .gitignore)"
    status: "pending"
  - id: "T6"
    content: "EIE PR-2: ruff 0.16.1 / mypy 2.3.0 / pytest 9.1.1 in pyproject [dev], requirements-ci.txt and pre-commit; pre-commit revs by SHA with tag comments; mirrors-mypy additional_dependencies equal to the CI type-check deps; pytest-unit hook drops `2>/dev/null || true`; Dependabot pip ignore for ruff/mypy/pytest (Core-owned); requires-python >=3.12 (CEG #409)"
    status: "pending"
  - id: "T7"
    content: "EIE PR-3: split the networked `validate_sdk_pin.py --verify-tag` step out of validate into an sdk-channel job that CI Gate reports but validate no longer waits on (CEG #412); keep the offline pin check in validate"
    status: "pending"
  - id: "T8"
    content: "EIE PR-4: tools/lock_requirements.sh renders seeded from the committed lock with a recorded --exclude-newer cutoff (tools/deps/exclude-newer.txt) and pinned uv; add --check (exit 1 on drift) plus a requirements-ci.lock hashed render; add scripts/install_locked.sh (--require-hashes) and switch all 13 requirements installs plus the pr-pipeline git+@v1 SDK install; add a lock --check step to sdk-channel; add a unit test proving --check fails on a stale lock (CEG #394/#419)"
    status: "pending"
  - id: "T9"
    content: "EIE PR-5: pin the CI Semgrep install; port CEG #418 precise float/singleton rules into .semgrep/semgrep-rules.yaml with fixtures under .semgrep/fixtures/, a .semgrepignore and a semgrep --test CI step"
    status: "pending"
  - id: "T10"
    content: "EIE PR-6: SHA-pin all 96 tag-pinned workflow `uses:` refs with `# vX` comments; the Dependabot github-actions ecosystem keeps them current"
    status: "pending"
  - id: "T11"
    content: "EIE PR-7, BLOCKED until explicit operator APPROVED (rules 90/93, Docker/deploy protection): docker-build.yml build-push-action `file: Dockerfile.prod`, so CI publishes the digest-pinned, hash-locked, non-root production image instead of the DEV image"
    status: "pending"
  - id: "T12"
    content: "Ledger: record session_debt findings for the CEG follow-ups (docker-build actions: read; dead baseline-ratchet caller), EIE V2 contract adoption, SonarCloud residual (Dockerfile + text:S8565) after PR-4, and Dockerfile curl/git runtime removal pending APPROVED"
    status: "pending"
```
