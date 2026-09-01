# FINAL FINDINGS — EIE Gate_SDK Adoption

## Executive Verdict

**GO for merge.** EIE runs the exact Gate_SDK revision consumed by green
Constellation.Gate PR #14, owns no Gate registration transport of its own, and
was proven against a real Gate process, a real EIE process, real HTTP and a
real PostgreSQL 16.

The adoption itself was small. Running it against the real rail was not: three
invariants this repository already recorded as proven did **not** hold, and one
of them was a cross-tenant data defect. All three are fixed and re-proven here.

| Claimed before | Actually observed | Now |
|---|---|---|
| `durable_before_completed: true` | forced persistence failure still answered `state="completed"`; Gate turned it into success; no row written | fixed, re-proven |
| `idempotency.tenant_scoped: true` | `UNIQUE(idempotency_key)` was global; one tenant's converge resolved to another tenant's stored row | fixed, re-proven |
| `real_postgres_proven: true` | `alembic upgrade head` logged success and committed nothing | fixed, re-proven |

## Repository / Branch / Candidate HEAD

| Field | Value |
|---|---|
| repository | Quantum-L9/Enrichment.Inference.Engine |
| branch | `claude/eie-gate-sdk-adoption-rpw476` |
| candidate HEAD | branch tip of `claude/eie-gate-sdk-adoption-rpw476`; last code-bearing commit `6e5ebb3cfc08b73400ada6d14d09eceac1c83017` |
| base | `origin/main` @ `cfda45043477bfe4a0f2a8c249ff9be30d1705aa` |
| python | 3.12.3 (matches CI; SDK requires >=3.12) |
| postgres | 16.13, real cluster, port 55432 |
| redis | real server, port 56379 |

The previously proven domain candidate `20a0e57de2d2db` no longer exists as a
commit: PR #200 was squash-merged into main as `10646ab`. This branch is based
on that merge, so the domain closure is inherited rather than re-derived.

## Gate_SDK Exact Revision / Provenance

Required and installed: `bfe6642062a85a720ad8c25e96446d4df1c299ac`.

Proven from the installed distribution's `direct_url.json`, not from the
resolver, in **both** processes on the rail:

```
EIE  : bfe6642062a85a720ad8c25e96446d4df1c299ac
Gate : bfe6642062a85a720ad8c25e96446d4df1c299ac
```

Pinned in `pyproject.toml` and `requirements-ci.txt`. Gate PR #14 pins the same
commit independently, so the release set is coherent by construction.

`scripts/validate_sdk_pin.py` still enforced the retired `a770e853` and only
checked three files, none of them `requirements-ci.txt` — it now checks that
file too and fails closed on a wrong pin in either.

**Retired findings.** These were correct at the previous EIE head and are now
closed by the installed artifact:

- ~~Gate_SDK cannot express `metadata.owner`~~ — `NodeRegistration.owner` is a
  first-class field; Gate received `metadata.owner: eie`.
- ~~Gate registration blocked on SDK capability~~ — `register_node()` exists and
  is used.
- ~~no coordinated SDK candidate exists~~ — `bfe6642` is that candidate.
- ~~pin bump pending for lack of candidate~~ — pinned.
- ~~`manual_http_remaining=true`~~ — deleted, and guarded.
- ~~`real_gate_runtime=NOT_RUN`~~ — run.

## Constellation.Gate PR #14 Revision

Exact head used: `ce34c9e8a91510fb96183cd14df058746f4bdec3`
("fix: clear the last two SonarCloud S5778 findings"). Materialised with
`git archive` and run as a real uvicorn process on 127.0.0.1:18000. Gate main
was **not** substituted.

## Canonical Converge Contract

Unchanged and re-proven on the real rail: `action=converge`, request
`EnrichRequest`, response `EnrichResponse`. No `entity_snapshot`, no top-level
`status`, no `final_fields`, no writeback in the response. No translation in
Gate, none in Gate_SDK, none introduced here. The pre-canonical compatibility
branch still exists for the `entity_snapshot` dialect and is not reached by the
canonical producer's payload.

## Gate Registration Migration

EIE supplies values; the SDK owns validation, rendering, retry and HTTP.

```
NodeRegistration(node_name="enrichment-engine", owner="eie",
                 node_type="enrichment", version="2.3.0",
                 health_endpoint="/api/v1/health",
                 supported_actions=("converge","graph-inference-result",
                                    "enrich","enrich-and-sync"),
                 timeout_ms=25000)
  -> await register_node(gate_url=..., registration=..., admin_token=..., overwrite=True)
```

Explicit lifecycle registration was **kept** (`auto_register_with_gate=False`),
per the contract's default straight-line move: only the transport moved. The
`_gate_registered` readiness signal remains authoritative, and registration
stays non-fatal to process startup while degrading readiness.

One deliberate value change: the advertised node cap is now EIE's own 25 s
ceiling rather than the SDK's 30 s default. Gate bounds a worker with
`min(remaining packet budget, node cap)`, so advertising 30 s would have told
Gate to wait on five seconds EIE has already abandoned.

**Payload equivalence** (semantic, not byte): `internal_url`,
`supported_actions`, `health_endpoint`, `metadata.owner`, `metadata.version`,
`metadata.type` all identical to the deleted client's body. The SDK adds
`metadata.generated_by=constellation-node-sdk` plus explicit `priority_class`,
`max_concurrent` and `timeout_ms`. Gate accepted all of them.

## Bespoke Registration Deletion

`app/services/gate_registration.py` is **deleted**, not renamed. EIE production
code now contains no `POST /v1/admin/register`, no `X-Admin-Token` construction,
no registration retry loop and no registration status-code taxonomy.

## Action Ownership / Routability

Read live from the running Gate:

```
registry: owner=eie  health_endpoint=/api/v1/health  timeout_ms=25000
          supported_actions=[converge, graph-inference-result, enrich, enrich-and-sync]
ready   : converge routable=true required_owner=eie resolved_node=enrichment-engine
```

`advertised (4) ⊂ runtime_allowed (10)` and `advertised ⊆ implemented`, both
asserted by tests. EIE does not advertise every action its runtime permits.

## Gate→EIE Real Runtime

Every component real: Gate process, EIE process, HTTP between them, PostgreSQL,
Gate_SDK `bfe6642` in both. No in-process fake worker, no SDK-only stand-in.
EIE registered itself through the SDK — it was **not** inserted into Gate's
registry by hand.

```
canonical TransportPacket -> real Gate /v1/execute -> validation ->
action ownership (converge -> eie) -> registry resolution ->
Gate-derived child packet -> GateDispatchTransport ->
real EIE /v1/execute -> SDK worker runtime -> handle_converge ->
required persistence -> EnrichResponse -> Gate response
```

Result: HTTP 200, `state="completed"`, response packet sourced from
`enrichment-engine`, child `timeout_ms=25000`.

## Effective Deadline Hierarchy

The defect: `handle_converge` called `Deadline.start(25)` unconditionally, so a
packet saying 2 s remained still opened a fresh 25 s operation — a second clock
over one request.

Now `effective_budget = min(EIE ceiling, packet budget)`, read off the packet
the SDK already offers a three-parameter handler. EIE reads exactly one field
(`header.timeout_ms`) and never decodes, validates or signs the packet.

Measured on the real rail — Gate's child budget, and the transport timeout EIE
then granted the provider:

| parent budget | child packet | provider attempt |
|---|---|---|
| 60 000 ms | 25 000 ms (node cap) | 20.0 s (attempt cap) |
| 30 000 ms | 25 000 ms (node cap) | 20.0 s (attempt cap) |
| 12 000 ms | 11 999 ms (remaining) | 10.0 s (budget − reserve) |
| 8 000 ms | 7 999 ms (remaining) | 6.0 s (budget − reserve) |
| 2 000 ms | — | answered in 0.05 s, no fresh 25 s operation |

The child is *slightly* under the parent because Gate passes the bounded
**remaining** budget with elapsed time subtracted. That is the bound working.

## Provider Retry / Timeout

Unchanged and re-verified. Gate whole-converge attempts: 1. SDK worker
transport attempts: 1 (EIE received exactly one `/v1/execute` dispatch per
converge). Provider retry owner: EIE. Perplexity SDK auto-retries: 0, observed
as `max_retries: 0` on every recorded provider call. Provider attempts draw
from the same remaining deadline, so a retry loop cannot multiply past the
caller's budget.

## Persistence Semantics

**This was the blocking defect.** `_persist_and_sync` was fire-and-forward and
the side-effect coordinator swallowed the persist exception, so a converge
whose PostgreSQL write failed still answered `state="completed"` with no
`failure_reason`. Gate turned that into a success. The enrichment was lost
silently. The once-per-key guard recorded the key anyway, so the retry that
would have recovered the work was suppressed as a duplicate.

Fixed: `require_persistence=True` on the canonical path re-raises
`PersistenceRequiredError`, no downstream effect fires, the key is **not**
recorded, and the handler answers a non-completed `EnrichResponse`.
Non-canonical callers keep the previous fire-and-forward behaviour.

Proven through the real rail against a real database outage:

```
store down  -> state="failed", reason "result not durable: ...", 0 rows
store up,   -> state="completed", exactly 1 row
same logical key, new transport packet
```

## Tenant Isolation

**Second blocking defect.** `enrichment_results.idempotency_key` carried a
global `UNIQUE` and was looked up by key alone. An idempotency key is
caller-chosen and unique only inside its own tenant, so two tenants using the
same string collided: the second tenant's converge resolved to the first
tenant's stored enrichment and was reported complete against a row it does not
own, its own write silently dropped.

Fixed in both places — the lookup now *requires* a tenant, and migration `002`
replaces `UNIQUE(idempotency_key)` with `UNIQUE(tenant_id, idempotency_key)`.
Verified independently through `psql`:

```
uq_enrichment_results_tenant_idempotency_key | UNIQUE (tenant_id, idempotency_key)
```

## Idempotency / Replay

All cases run through the real Gate, observed in PostgreSQL:

| case | result |
|---|---|
| same tenant + same logical run | 1 durable result |
| new transport packet + same logical operation id | same durable operation |
| same tenant + same entity + new logical run | distinct operation |
| different tenant + same raw key | separate operation, isolated |
| no logical key | keyed by own identity, not collapsed by packet_id |

`packet_id` is not the business key. Entity-only fallback was not
reintroduced.

A harness note worth recording: Gate dedups on the **transport** idempotency
key and answers a repeat from its own cache without ever calling EIE. A replay
test that reuses the transport key proves nothing about EIE. The retry above
uses a fresh transport packet carrying the same logical key.

## Handler Transport Metadata (§17 re-audit against bfe6642)

The previous finding recorded that the SDK's two-parameter handler invocation
exposed only `tenant` and `payload`, never the packet header. Re-audited against
the exact installed revision, that is still true *of the two-parameter form* —
but it is a property of the arity the handler chooses, not a limit of the SDK:

```
len(parameters) == 1  -> handler(packet)
len(parameters) == 2  -> handler(packet.tenant.org_id, packet.payload)
len(parameters) == 0  -> handler()
otherwise             -> handler(packet.tenant.org_id, packet.payload, packet)
```

`handle_converge` now declares three parameters, so it receives the packet. That
is what closes the deadline seam — EIE reads `header.timeout_ms` off it — and it
means the transport idempotency key is *available* to the handler. It is
deliberately not used as the business key.

```yaml
handler_receives_transport_idempotency_directly: true   # via the 3-param form
domain_idempotency_source: EnrichRequest.idempotency_key  # payload, caller-supplied
packet_id_used_as_business_key: false
transport_key_read_by_the_domain: false
```

The domain operation ID stays `EnrichRequest.idempotency_key`, supplied by the
canonical producer, and the durable row is keyed `(tenant_id, idempotency_key)`.
The transport key is Gate's dedup key and is a different thing: Gate answers a
repeat of the same transport key from its own cache without ever calling EIE,
which is exactly why a replay test must mint a new transport packet carrying the
same logical key. Deriving the domain ID from `packet_id` would make every
retry a new operation and defeat the durability guarantee above.

This is informational, as the contract specifies: nothing here requires a
change, because the canonical producer supplies the logical key.

## Concurrent Same-Key State

Six concurrent canonical converges sharing one logical key through the real
Gate: all answered `completed`, exactly **one** durable row.

## Graph State

Zero synchronous Graph calls on the canonical path, instrumented on the real
run rather than inferred from `graph_sync=False`: `graph_synced=0`,
`side_effect_graph_sync_excluded=1` per canonical converge.

## Writeback Boundary

Unchanged. No writeback in the canonical response; no writeback action is
advertised to Gate.

## Real PostgreSQL Evidence

Real cluster, migration head applied and verified independently through `psql`
(5 tables + `alembic_version=002`). Successful persistence independently
visible; failed persistence independently absent; same-key concurrency safe;
cross-tenant same raw key isolated.

**Third defect, found here.** `alembic upgrade head` committed nothing:
`run_async_migrations` configured the context in one `run_sync` call and ran
migrations in a second lambda that discarded its connection, with no
`context.begin_transaction()` anywhere. Alembic logged
`Running upgrade -> 001` and exited 0 against a database with zero tables.
Shape now follows alembic's own `templates/async/env.py`.

## Real Provider Evidence

`real_provider: NOT_RUN.` No Perplexity credential is available in this
environment and none was required: the seam is installed at the outermost
vendor object (`perplexity_client._clients[key]`) only. Gate, the SDK runtime,
the handler, the deadline, persistence and the response are all real. This is
**not** claimed as a full production runtime.

## Architecture Drift Guards

`tests/architecture/test_no_bespoke_gate_transport.py` — 9 guards over
`app/` only (the SDK is expected to contain these constructs):
no `/v1/admin/register`, no `X-Admin-Token` construction, no reintroduced
registration module, registration must come from `constellation_node_sdk`, and
no locally implemented `TransportPacket`, transport validation, signing,
hashing or `/v1/execute` route.

**Mutation-checked.** Reintroducing a direct registration `POST` failed 2
guards naming the offending file; the mutation was then removed and all 9 pass.
The migration guard and the tenant-scoping guards were mutation-checked the
same way against their exact original shapes.

## Tests Actually Executed

| command | result |
|---|---|
| `pytest` (full suite, coverage) | **1639 passed, 4 xfailed, 74.53%** (gate 71%) |
| `ruff check .` | PASS |
| `ruff format --check .` | PASS (311 files) |
| `make verify` (contract manifest) | PASS — 10/10 |
| `scripts/validate_sdk_pin.py` | PASS |
| SDK provenance from `direct_url.json` (both processes) | PASS |
| SDK registration payload equivalence | PASS |
| real Gate registration | PASS |
| real Gate routability | PASS |
| real Gate→EIE HTTP round trip | PASS |
| remaining-deadline propagation (4 budgets) | PASS |
| real persistence success through Gate | PASS |
| persistence failure through Gate | PASS |
| logical replay through Gate | PASS |
| concurrent same-key through Gate | PASS |
| zero-sync-Graph through Gate | PASS |
| architecture no-bespoke-registration guard | PASS (mutation-checked) |
| `mypy` (advisory) | 44 errors — **identical count on unmodified main** |
| CI: Test Suite / pytest+coverage | PASS |
| CI: L9 Constitution Gate, Contract-Bound Change Gate | PASS |
| CI: SonarCloud quality gate | PASS — "0 New issues" |
| CI: Baseline Ratchet (all four jobs) | PASS |
| CI: Security Scanning | **FAIL** — external, see the release blocker below |

## make pr Result

**FAIL — repository automation absent.** `make pr` runs
`bash local_pr_pipeline/pr_pipeline.sh all`. That file does not exist and has
never existed in any branch of this repository (`git log --all` over the path
returns nothing); the `local_pr_pipeline/` directory contains six other
scripts. The previous finding stands, unchanged.

No replacement pipeline was invented. The repository-authoritative gates above
were run instead, as additional evidence — not as a claim that `make pr` passed.

## Remaining Blocking Defects

None. The three found during this work are fixed and re-proven.

## Remaining Non-Blocking Defects

1. **`make pr` is dead** — `local_pr_pipeline/pr_pipeline.sh` absent. Repository
   automation gap, not a code defect. Owner: repo maintainers.
2. **Redis `IdempotencyStore` is not tenant-scoped** (`enrich:idem:{key}`).
   Reconfirmed **off** the canonical converge path by runtime trace: after ~40
   canonical converges through the real rail, zero `enrich:idem:` keys exist.
   It is reachable via the `enrich` action, which EIE advertises. Reported, not
   fixed — per contract §24, evidence decides and the evidence keeps it off
   canonical converge.
3. **`mypy` 44 errors** — unchanged from main, advisory per `CLAUDE.md`. Mostly
   missing stubs (`yaml`, `aiofiles`) and `no-any-return`.
4. **`requires-python = ">=3.11"`** in `pyproject.toml` is looser than reality:
   the SDK requires `>=3.12` and CI runs 3.12/3.13, so a 3.11 install fails at
   resolution. Pre-existing; left alone to avoid widening this PR.
5. **EIE side-effect packets trip Gate's replay guard.** After a canonical
   converge, EIE's score-invalidate and event legs call back into Gate and one
   is rejected `400 replay detected`. Off the canonical response path
   (fire-and-forward, after the answer) and benign in this harness, where no
   CEG node is registered to own `score-invalidate`. Worth a look; not this
   PR's scope.
6. **`alembic` is not a declared dependency.** `alembic.ini` and `migrations/`
   exist and `alembic upgrade head` is the documented way to apply the schema,
   but `alembic` appears in no `pyproject.toml` extra and no `requirements*.txt`.
   A fresh install therefore cannot run migrations, and CI has no alembic at
   all — which is how the migration guard, written to execute the module,
   failed there. The guard is now static; the missing declaration is not fixed
   here because adding a dependency is a wider call than this PR should make.
7. **CI installs a third Gate_SDK revision.** `.github/workflows/pr-pipeline.yml`
   hard-codes `Gate_SDK.git@ead0f48166f510683e9dec6ff7383258cc4307f2` for the
   packet/envelope gates — neither the old pin nor `bfe6642`. So the gate step
   validates against an SDK the repository does not pin, and
   `scripts/validate_sdk_pin.py` does not look at workflow files, so the drift
   is invisible to it. Incidentally this is what proves the `cryptography`
   finding above: `ead0f481` carries no upper bound and resolves to
   `cryptography 50.0.1` in the very same CI run where `bfe6642` resolves to the
   vulnerable `44.0.3`.

## External Release Blockers

## External Release Blocker — Gate_SDK caps `cryptography` on a vulnerable line

**Found by CI on this PR, and introduced by this PR.** `Security Scanning`
(`pip-audit`) **passes on `main`** and fails here.

`bfe6642` added an upper bound its predecessor did not have:

| revision | constraint | resolves to | pip-audit |
|---|---|---|---|
| `a770e853` (old) | `cryptography>=43.0.0` | latest | clean |
| `bfe6642` (new) | `cryptography>=43.0.0,<45` | `44.0.3` | **7 known vulnerabilities** |

Fixed upstream in `46.0.5` / `46.0.6` / `48.0.1` / `49.0.0` / `50.0.0` —
every fix is above the SDK's ceiling. Highest-impact is PYSEC-2026-3552, a
Bleichenbacher oracle against the content-encryption key in `pkcs7_decrypt_*`,
introduced in 44.0.0 and fixed in 50.0.0.

**EIE cannot fix this.** Adding `cryptography>=46` to EIE's own dependencies is
not a workaround, it is unresolvable:

```
ERROR: Cannot install constellation-node-sdk==1.0.1 and cryptography>=46
       because these package versions have conflicting dependencies.
ERROR: ResolutionImpossible
```

Pinning around it would mean forking the SDK revision, which breaks the one
property this whole change exists to establish: Gate and EIE running the *same*
SDK commit. So it is reported rather than papered over, per the contract's stop
rule.

```
status:                  BLOCKED (external)
blocking_invariant:      no known-vulnerable dependency in the release set
evidence:                pip-audit, 7 findings, cryptography 44.0.3;
                         Security Scanning green on main, red here;
                         ResolutionImpossible when EIE tries to override
smallest_required_change: relax `cryptography>=43.0.0,<45` in Gate_SDK
                         pyproject.toml to admit >=50, cut a new SDK revision,
                         and re-pin Gate PR #14 and EIE together
owner:                   Quantum-L9/Gate_SDK
```

This does not affect any behaviour proven above: the transport, registration,
deadline, persistence and idempotency evidence all stand. It is a supply-chain
constraint on the revision, not a defect in the integration.

Also: Constellation.Gate PR #14 is not yet merged; this branch is proven against
its exact head, so the two must land together or Gate first.

## Scope Drift Audit

Three fixes were outside the literal adoption brief and each was forced by a
contract requirement that could not otherwise be satisfied:

- `alembic env.py` — §20 requires "migration head applies". It did not.
- durable-before-completed — §15 requires the forced-failure proof. It failed.
- tenant-scoped idempotency — §16 requires cross-tenant isolation. It failed.

The SDK pin validator was updated because it enforced the pin this task
replaces. No enrichment redesign was attempted. The compatibility branch,
convergence loop, provider client, result store shape and Graph boundary are
untouched.

## Merge Recommendation

**APPROVE.**

## Release-Set Recommendation

**BLOCKED**, on Gate_SDK — not on EIE. The integration is proven; the release
set carries 7 known `cryptography` vulnerabilities that only Gate_SDK can lift,
and it must then be re-pinned in Gate PR #14 and here together. Sequencing
behind Gate PR #14 still applies.

## Next Straight-Line Move

Bring IB-Odoo_19 onto this same exact Gate_SDK revision, delete its remaining
shadow transport, then execute the complete real
Odoo → Gate → EIE → PostgreSQL → Gate → Odoo release rail.

## Machine-Readable Summary

```yaml
repository: Quantum-L9/Enrichment.Inference.Engine
branch: "claude/eie-gate-sdk-adoption-rpw476"
candidate_head: "branch tip of claude/eie-gate-sdk-adoption-rpw476"
last_code_commit: "6e5ebb3cfc08b73400ada6d14d09eceac1c83017"
gate_sdk:
  required_sha: "bfe6642062a85a720ad8c25e96446d4df1c299ac"
  installed_sha: "bfe6642062a85a720ad8c25e96446d4df1c299ac"
  provenance: PASS
registration:
  sdk_owned: true
  api: register_node
  owner: eie
  health_endpoint: /api/v1/health
  bespoke_http_remaining: false
  gate_accepted: PASS
canonical:
  action: converge
  request: EnrichRequest
  response: EnrichResponse
  alternate_dialect_present: false
gate_runtime:
  gate_pr: 14
  gate_head: "ce34c9e8a91510fb96183cd14df058746f4bdec3"
  real_gate_process: PASS
  real_eie_process: PASS
  real_http_transport: PASS
  converge_routable: PASS
deadline:
  eie_ceiling_seconds: 25
  respects_smaller_packet_budget: true
  resets_budget: false
  provider_attempt_uses_remaining_budget: true
persistence:
  required_before_completed: true
  failure_propagates: true
  completion_on_failure: false
  real_postgres: PASS
idempotency:
  handler_receives_transport_idempotency_directly: true
  domain_idempotency_source: EnrichRequest.idempotency_key
  logical_operation_primary: true
  tenant_scoped: true
  packet_id_primary: false
  concurrent_same_key_safe: true
  real_gate_replay: PASS
provider:
  retry_owner: EIE
  sdk_auto_retries: 0
  live_provider: NOT_RUN
  deterministic_provider_seam: PASS
graph:
  canonical_sync_calls: 0
validation:
  full_suite: PASS
  contracts: PASS
  real_postgres: PASS
  real_gate_eie: PASS
  lint: PASS
  format: PASS
  make_pr: FAIL
  security_scan: FAIL (external: Gate_SDK cryptography ceiling, see above)
blocking_defects: []
non_blocking_defects:
  - "make pr dead: local_pr_pipeline/pr_pipeline.sh absent, never existed"
  - "Redis IdempotencyStore not tenant-scoped; off canonical converge by runtime trace"
  - "mypy 44 errors, identical count on unmodified main, advisory"
  - "requires-python >=3.11 looser than the SDK's >=3.12 and CI's 3.12"
  - "EIE side-effect packets trip Gate replay guard, off the canonical response path"
  - "alembic is not a declared dependency though alembic.ini and migrations/ exist"
  - "CI pr-pipeline.yml hard-codes a third Gate_SDK revision (ead0f481), unseen by validate_sdk_pin.py"
external_release_blockers:
  - "Gate_SDK bfe6642 caps cryptography <45, pinning EIE to 44.0.3 with 7 known
     vulnerabilities (PYSEC-2026-3552 et al, fixed in 46.0.5-50.0.0). pip-audit
     is green on main and red here. EIE cannot override it: ResolutionImpossible.
     Owner Gate_SDK; smallest fix is relaxing the ceiling and re-cutting the
     revision Gate PR #14 and EIE both pin."
  - "Constellation.Gate PR #14 not yet merged; must land together or Gate first"
verdict:
  local: GO
  supply_chain: BLOCKED_EXTERNAL (Gate_SDK cryptography ceiling)
  domain_contract: GO
  registration: GO
  runtime: GO
  merge: APPROVE (the integration is sound; the dependency ceiling is Gate_SDK's)
  release_set: BLOCKED until Gate_SDK relaxes the cryptography ceiling
next_move: >
  Bring IB-Odoo_19 onto this same exact Gate_SDK revision, delete
  its remaining shadow transport, then execute the complete real
  Odoo -> Gate -> EIE -> PostgreSQL -> Gate -> Odoo release rail.
```
