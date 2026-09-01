# EIE SDK-ADOPTION FINDINGS BRIEF

```
HEAD:
  candidate: branch tip of claude/eie-gate-sdk-adoption-rpw476
  last_code: 6e5ebb3cfc08b73400ada6d14d09eceac1c83017
  base:      origin/main @ cfda45043477bfe4a0f2a8c249ff9be30d1705aa

GATE_SDK:
  required_sha: bfe6642062a85a720ad8c25e96446d4df1c299ac
  installed_sha: bfe6642062a85a720ad8c25e96446d4df1c299ac
  provenance:   PASS (direct_url.json of the installed distribution, in BOTH
                EIE and Gate processes; not the resolver)

REGISTRATION:
  sdk_owned:            true (NodeRegistration + register_node)
  bespoke_http_deleted: true (app/services/gate_registration.py removed)
  owner:                eie   (SDK-rendered metadata.owner)
  actions:              converge, graph-inference-result, enrich, enrich-and-sync
                        advertised(4) subset of runtime_allowed(10)
  Gate_acceptance:      PASS  (POST /v1/admin/register -> 200 OK)

GATE #14:
  exact_head:       ce34c9e8a91510fb96183cd14df058746f4bdec3
  real_process:     PASS (uvicorn, 127.0.0.1:18000, SDK bfe6642)
  converge_routable: PASS (routable=true required_owner=eie
                     resolved_node=enrichment-engine)
  real_dispatch:    PASS (Gate-derived child packet over real HTTP to
                    EIE /v1/execute; exactly 1 dispatch per converge)

DOMAIN:
  canonical_contract:       PASS (EnrichRequest in / EnrichResponse out,
                            untranslated; no alternate dialect on this path)
  durable_before_completed: PASS (after fix - see BLOCKERS FOUND)
  tenant_idempotency:       PASS (after fix - see BLOCKERS FOUND)
  concurrency:              PASS (6 concurrent same-key -> 1 durable row)
  zero_sync_graph:          PASS (graph_synced=0, instrumented not inferred)

DEADLINE:
  packet_budget:        Gate writes bounded remaining budget to header.timeout_ms
  EIE_effective_budget: min(EIE 25s ceiling, packet budget)
                        60s->25000ms  30s->25000ms  12s->11999ms  8s->7999ms
  provider_budget:      derived from the same deadline
                        30s->20.0s  12s->10.0s  8s->6.0s  (budget - 2s reserve)
  reset_detected:       NO (2s packet answered in 0.05s; no fresh 25s operation)

REAL RUNTIME:
  Gate:       PASS (real Constellation.Gate PR #14 process)
  EIE:        PASS (real EIE process, SDK worker runtime)
  PostgreSQL: PASS (real cluster 16.13; migration head 002; verified via psql)
  provider:   NOT_RUN (no credential; deterministic seam at the outermost
              Perplexity SDK client object only - NOT a full production runtime)

CI (final state):
  Test Suite:                 SUCCESS
  Test (pytest + coverage):   SUCCESS
  L9 Constitution Gate:       SUCCESS
  Contract-Bound Change Gate: SUCCESS
  SonarCloud:                 SUCCESS - "Quality Gate passed, 0 New issues"
  Baseline Ratchet:           SUCCESS (Required Tests, Quarantined Debt,
                              Workflow Integrity, Verdict)
  PR Size & Review Policy:    SUCCESS (the bot's "BLOCKED" comment is advisory
                              text; its own check passes)
  Security Scanning:          FAILURE - external, Gate_SDK cryptography ceiling
                              (green on main; see EXTERNAL RELEASE DEBT)
  CI Gate / PR Pipeline Gate: FAILURE - aggregators only; both fail on
                              "security: failure" and nothing else
  everything else:            SUCCESS

  Four CI rounds were needed. Every failure except the cryptography one was
  this PR's and was fixed, not worked around:
   1. Contract-Bound Change Gate - contract-bound surfaces changed with no
      corresponding tests/contracts change. Closed by a real contract test.
   2. SonarCloud - a malformed "# NOSONAR(S5332)" suppression, which is why
      Sonar reported the suppression syntax AND the rule it was meant to
      suppress; plus two over-broad pytest.raises blocks.
   3. Test Suite - the migration guard executed the migration module, which
      imports alembic, undeclared in this project and absent in CI; and the
      contract test imported SQLAlchemy at module scope, breaking collection
      in the constitution gate's SDK-only environment.
   4. github-code-quality - a bare "await commit" introduced by fix 2. Hoisting
      the argument-constructing calls satisfies both rules at once.
  Several earlier "failures" were cancellations from my own pushes superseding
  in-flight runs; the ratchet is fail-closed on cancellation, so they cleared
  once a run completed un-superseded.

MAKE PR:
  result:             FAIL
  failed_phase_if_any: phase 0 - the pipeline script itself is absent.
                       "make pr" runs 'bash local_pr_pipeline/pr_pipeline.sh all';
                       that file does not exist and has never existed in any
                       branch (git log --all over the path returns nothing).
                       Error 127. No replacement pipeline was invented.

REMOTE PR:
  created:     yes
  number:      201
  url:         https://github.com/Quantum-L9/Enrichment.Inference.Engine/pull/201
  remote_head: 6e5ebb3cfc08b73400ada6d14d09eceac1c83017
  ci_rounds:   round 1 failed "Contract-Bound Change Gate" - this branch changed
               app/engines/ and app/services/ with no corresponding change under
               docs/contracts/, tests/contracts/ or the constitution verifier.
               The gate was right: both fixed behaviours are contract-level.
               Closed by tests/contracts/test_durability_and_tenant_contract.py,
               which pins what a caller is promised rather than restating the
               unit mechanics. Not worked around.

TEST EVIDENCE:
  - command: pytest (full suite, coverage)
    result:  PASS - 1639 passed, 4 xfailed, 74.53% (gate 71%)
  - command: ruff check .
    result:  PASS
  - command: ruff format --check .
    result:  PASS (311 files)
  - command: make verify (contract manifest)
    result:  PASS - 10/10 active contracts
  - command: scripts/validate_sdk_pin.py
    result:  PASS (updated to bfe6642; now also checks requirements-ci.txt;
             mutation-checked to fail closed on a wrong pin)
  - command: mypy app
    result:  44 errors - IDENTICAL count on unmodified main; advisory per CLAUDE.md
  - command: alembic upgrade head + psql verification
    result:  PASS - 5 tables + alembic_version=002 (did NOT hold before the fix)
  - command: real Gate registration + /v1/registry + /v1/ready
    result:  PASS - owner=eie, timeout_ms=25000, health /api/v1/health, routable
  - command: real Gate -> EIE canonical converge
    result:  PASS - HTTP 200, state=completed, source_node=enrichment-engine
  - command: forced persistence failure through the real rail
    result:  PASS - state=failed, no row (returned "completed" before the fix)
  - command: logical replay, new transport packet, same logical key
    result:  PASS - persists exactly once after the store recovers
  - command: cross-tenant same raw key
    result:  PASS - separate rows (leaked across tenants before the fix)
  - command: architecture guards (9) + mutation check
    result:  PASS - rogue registration POST fails 2 guards by name

BLOCKERS:
  - none outstanding. Three were FOUND and FIXED during this work, each one an
    invariant this repo already recorded as proven:
    1. completed did not mean durable - a converge whose PostgreSQL write failed
       still answered state="completed" with no failure_reason; Gate turned it
       into success; the enrichment was lost silently, and the once-per-key guard
       then suppressed the retry that would have recovered it.
    2. idempotency was not tenant-scoped - UNIQUE(idempotency_key) was global, so
       one tenant's converge resolved to another tenant's stored row: a
       cross-tenant read whose own write was dropped and reported complete.
       Migration 002 replaces it with UNIQUE(tenant_id, idempotency_key).
    3. alembic upgrade head committed nothing - configure and run were split
       across two run_sync calls with no context.begin_transaction(); alembic
       exited 0 against a database with zero tables.

NON_BLOCKING:
  - make pr is dead (local_pr_pipeline/pr_pipeline.sh absent). Repo automation gap.
  - Redis IdempotencyStore key enrich:idem:{key} is not tenant-scoped.
    RECONFIRMED off the canonical converge path by runtime trace: after ~40
    canonical converges, zero enrich:idem: keys exist. Reachable via the
    "enrich" action. Reported, not fixed, per contract section 24.
  - mypy 44 errors, unchanged from main, advisory.
  - requires-python ">=3.11" is looser than the SDK's ">=3.12" and CI's 3.12.
  - EIE post-response side-effect packets trip Gate's replay guard; off the
    canonical response path.
  - alembic is not declared in pyproject.toml or any requirements file, though
    alembic.ini and migrations/ exist and "alembic upgrade head" is the
    documented way to apply the schema. A fresh install cannot run migrations.
  - .github/workflows/pr-pipeline.yml hard-codes a THIRD Gate_SDK revision
    (ead0f481...) for the packet/envelope gates - neither the old pin nor
    bfe6642 - and validate_sdk_pin.py does not inspect workflow files, so that
    drift is invisible to it. It is also the control that proves the
    cryptography finding: ead0f481 has no ceiling and pulls cryptography 50.0.1
    in the same CI run where bfe6642 pulls the vulnerable 44.0.3.

EXTERNAL RELEASE DEBT:
  - BLOCKER, owned by Gate_SDK. bfe6642 added "cryptography>=43.0.0,<45"; the
    predecessor a770e853 had no upper bound. That ceiling resolves EIE to
    cryptography 44.0.3, which pip-audit reports with 7 known vulnerabilities
    (PYSEC-2026-3552, a Bleichenbacher oracle in pkcs7_decrypt_*, plus 6 more;
    fixes land in 46.0.5 / 46.0.6 / 48.0.1 / 49.0.0 / 50.0.0 - all above the
    ceiling). Introduced by THIS PR: "Security Scanning" is green on main and
    red here. EIE cannot fix it - adding cryptography>=46 gives
    ResolutionImpossible against the SDK, and pinning around it would fork the
    revision, destroying the one-SDK-across-the-rail property this change
    exists to establish. Smallest required change: relax the ceiling in
    Gate_SDK, cut a revision, re-pin Gate PR #14 and EIE together.
    Does not affect any behaviour proven above.
  - Constellation.Gate PR #14 is not yet merged. This branch is proven against
    its exact head (ce34c9e8), so the two land together or Gate first.

SCOPE DRIFT:
  Three fixes fell outside the literal adoption brief; each was forced by a
  contract requirement that could not otherwise be satisfied - section 20
  (migration head applies), section 15 (durable-before-completed), section 16
  (tenant isolation). The SDK pin validator was updated because it enforced the
  very pin this task replaces. No enrichment redesign: the compatibility branch,
  convergence loop, provider client, result-store shape and Graph boundary are
  untouched.

VERDICT:
  local:        GO
  domain:       GO
  registration: GO
  runtime:      GO
  merge:        APPROVE
  release_set:  BLOCKED on Gate_SDK (cryptography ceiling), then sequenced
                behind Constellation.Gate PR #14. EIE's own integration is GO.

NEXT STRAIGHT_LINE_MOVE:
  Update IB-Odoo_19 to exact Gate_SDK bfe6642, remove its shadow transport,
  then execute the four-repository real runtime rail.
```
