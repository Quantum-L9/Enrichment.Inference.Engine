# EIE PR FINDINGS BRIEF

```
REPOSITORY:
  name: Quantum-L9/Enrichment.Inference.Engine
  branch: claude/eie-domain-closure-86oi52
  candidate_head_sha: 0356ef3e4b7f251a948215a3379624f34fbc357b
  base: origin/main @ cfda45043477bfe4a0f2a8c249ff9be30d1705aa

MAKE PR:
  result: FAIL — could not execute
  failing_phase: invocation (before any gate ran)
  detail: >
    `make pr` runs `bash local_pr_pipeline/pr_pipeline.sh all`. That script does
    not exist. It is absent from origin/main and from the entire git history of
    this repository (`git log --all -- local_pr_pipeline/pr_pipeline.sh` is
    empty), as is local_pr_pipeline/docker-compose.pr.yml, which `pr-services-up`
    references. Every `make pr*` target is therefore dead. This is pre-existing
    and was NOT caused by this change.
  not_suppressed: >
    The failure is reported as-is rather than worked around. Authoring a
    replacement pipeline would mean inventing the phase contract (validate /
    lint / semgrep / test / security / compliance / l9 / docs) that the missing
    script defines — a substantial harness this task has no basis to specify.
  substitute_gates_run: >
    `make agent-check` — CLAUDE.md's "THE universal gate" — plus each of its
    eight phases individually, and the full pytest suite against a real
    PostgreSQL 16.13 server. Results below.

REMOTE PR:
  created: no
  number: null
  url: null
  remote_head_sha: null
  base: null
  note: >
    No pull request was requested and none was opened. The branch is committed
    locally; publication is the operator's call.

VERDICT:
  canonical_contract: GO — one EnrichRequest/EnrichResponse protocol, adapter deleted
  persistence: GO — required persistence is durable before `completed`
  tenant_isolation: GO — UNIQUE(tenant_id, idempotency_key), tenant-scoped lookups
  idempotency: GO — logical operation identity; no packet or entity fallback
  concurrency: GO — resolved at the database, proven with a real race
  deadline: GO — 25 s shared monotonic deadline preserved unchanged
  provider_runtime: GO — EIE sole retry owner, SDK auto-retries 0, preserved
  graph: GO — zero synchronous Graph calls on canonical converge, preserved
  gate_sdk: PENDING — pin unchanged; no coordinated transport release set exists
  registration: BLOCKED_EXTERNAL_SDK_CAPABILITY — Gate_SDK emits no metadata.owner
  local: GO
  merge: APPROVE
  release_set: PENDING

IMPLEMENTED:
  - Deleted the pre-canonical entity_snapshot dialect (adapter + compat branch + its tests)
  - Relocated DEFAULT_CONVERGE_TIMEOUT_SECONDS and ODOO_COMPLETED_STATE to canonical homes
  - Required vs optional side effects; RequiredSideEffectError on failed persistence
  - Canonical converge answers a required-persistence failure with a non-completed EnrichResponse
  - Completion marker written only when persistence actually committed
  - Logical-operation dedupe key; removed the entity-hash fallback entirely
  - UNIQUE(tenant_id, idempotency_key) + tenant-scoped lookups (migration 002)
  - IntegrityError race resolution: the loser reads back the winner as an idempotent hit
  - Per-key asyncio lock, documented as an optimisation over the DB constraint
  - Fixed migrations/env.py: migrations were never committed (silent no-op)
  - Fixed duplicate ix_convergence_runs_state (create_all failed on a real server)
  - Re-stamped app/engines/handlers.py in the L9 enrichment manifest

PROVEN:
  - 1584 passed, 4 xfailed, 74.70% coverage (baseline was 1577 / 74.32%)
  - 9 real-PostgreSQL gates, each verified non-vacuous against pre-fix code
  - Migration 001 -> insert -> 002 preserves all rows; cross-tenant key goes
    rejected -> accepted; same-tenant duplicate stays rejected; NULL keys stay
    unconstrained; 002 -> 001 -> 002 round-trip clean
  - Downgrade REFUSES loudly while cross-tenant duplicates exist (no data loss)
  - Legacy consumer inventory across 4 repositories: no active consumer
  - Gate_SDK metadata.owner gap confirmed against installed 1.0.1 AND current main
  - Action matrix: advertised subset-of implemented subset-of runtime_allowed

BLOCKERS:
  - none blocking merge

NON_BLOCKING:
  - Redis IdempotencyStore key `enrich:idem:{key}` is not tenant-scoped (same
    defect family as the DB one; not on the canonical converge path)
  - packet_id never reaches handle_converge — the SDK passes only
    (tenant, payload) to a 2-parameter handler — so the pkt: fallback is dead
    on this path
  - mypy: 44 errors in 23 files. IDENTICAL on origin/main; advisory per AGENTS.md
  - audit_engine --strict: 10 CRITICAL. IDENTICAL set on origin/main; exit 1
    on both, so agent-check gates 3 and 6 are red on main independently of this
    change
  - make pr / make pr-* targets are dead repo-wide (see MAKE PR above)

LEGACY DIALECT:
  active_consumers: none
  searched: Enrichment.Inference.Engine, IB-Odoo_19, Constellation.Gate, Gate_SDK
  evidence: >
    IB-Odoo_19's sole ConvergeRequest builder emits canonical only and asserts
    the dialect's absence in its own test; Gate_SDK and Constellation.Gate
    reference it only in transport-neutrality tests asserting it must NOT
    appear; remaining hits are documentation.
  disposition: DELETED — a legacy payload now fails validation visibly rather
    than being silently rewritten

PERSISTENCE:
  durable_before_completed: yes
  failed_persistence_retryable: yes — no completion marker, retry re-runs and succeeds
  tenant_scoped_key: yes — enforced by the database, not only in process
  concurrent_same_key: exactly one durable row; both callers get that row's id

GATE_SDK:
  exact_sha: a770e8531dc1c59ce01e1dbb0f4162785d9dda89
  installed_package: constellation-node-sdk 1.0.1, verified from the same SHA
  registration_sdk_owned: no
  bespoke_registration_remaining: yes — app/services/gate_registration.py
  reason: >
    Constellation.Gate refuses a canonical action without metadata.owner;
    Gate_SDK build_registration_payload emits only version/type/generated_by.
    Migrating today would have Gate reject `converge` while the node reported
    healthy. test_sdk_still_cannot_express_owner FAILS when the SDK closes the
    gap, which is the trigger to delete the local HTTP.

TEST EVIDENCE:
  - command: pytest tests/ (with EIE_TEST_DATABASE_URL)
    result: 1584 passed, 4 xfailed, coverage 74.70% (floor 71%)
  - command: pytest tests/integration/test_persistence_idempotency_postgres.py
    result: 9 passed against real PostgreSQL 16.13
  - command: same, against the pre-fix global-UNIQUE schema
    result: 2 failed — gates are non-vacuous
  - command: same, without the IntegrityError race resolution
    result: 1 failed — race gate is non-vacuous
  - command: pytest tests/unit/test_durable_before_completed.py
    result: 10 passed
  - command: pytest tests/contracts/test_gate_registration_boundary.py
    result: 4 passed
  - command: ruff check . / ruff format --check .
    result: PASS / 308 files already formatted
  - command: pytest tests/unit/ tests/compliance/
    result: 165 passed
  - command: pytest tests/ci/
    result: 17 passed
  - command: python tools/verify_contracts.py
    result: PASS — 10/10 active contracts
  - command: python tools/payload_contract_compiler.py --stdout-only
    result: PASS
  - command: mypy app
    result: 44 errors — identical count and files on origin/main
  - command: python tools/audit_engine.py --strict
    result: exit 1, 10 CRITICAL — identical CRITICAL set on origin/main
  - command: make pr
    result: FAIL — local_pr_pipeline/pr_pipeline.sh does not exist (pre-existing)

REAL RUNTIME:
  postgres: REAL — PostgreSQL 16.13 run directly (Docker daemon unavailable);
            no mock, SQLite, or in-memory substitute
  gate: NOT_RUN — no Constellation.Gate instance was exercised. The cross-repo
        proof is SDK-shaped and structural, and is reported as such rather than
        presented as a real-Gate round trip.
  provider: NOT_RUN — no live Perplexity call; provider retry/timeout ownership
        was preserved unchanged, not re-proven

SCOPE DRIFT:
  none: >
    No queue, outbox, scheduler, provider framework, state machine, transport,
    routing system, or distributed lock was introduced. Three fixes fall outside
    the literal task list and are in-scope-on-identification: the alembic no-op
    (without it migration 002 could not be proven, or trusted in production),
    the duplicate index (blocks schema creation on a real server), and the
    migration's name[]/text[] cast (found by running it).

NEXT_STRAIGHT_LINE_MOVE: >
  Land metadata.owner in Gate_SDK's build_registration_payload. It is the single
  change that unblocks the registration migration, and the EIE tripwire turns
  red the moment it ships, so the EIE follow-up is a deletion with an automatic
  trigger rather than a remembered chore.
```
