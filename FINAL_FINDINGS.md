# FINAL_FINDINGS — EIE Canonical Enrichment Domain Closure

## Executive Verdict

The enrichment domain is closed at the contract boundary and, for the first
time, below it. `converge` now has one production protocol; required
persistence is durable before `completed` is acknowledged; domain idempotency
is keyed on the logical operation and scoped to the tenant in the database as
well as in process; and a concurrent same-key race is resolved at the database,
not by a process-local set that a second worker never shares.

Two items are **not** closed and are external, not deferred by choice:
Gate_SDK still cannot express `metadata.owner`, so EIE's bespoke Gate
registration must stay; and the Gate_SDK pin is unchanged pending a coordinated
transport release set. Both are gated executably rather than described.

Three pre-existing defects were found while proving the work and fixed in
scope. One of them — `alembic upgrade` silently applying nothing — meant no
migration this repository has ever shipped could be trusted to have run.

**Local verdict: GO. Merge: APPROVE. Runtime: PROOF_PENDING (real Gate not
exercised).**

## Repository / Branch / Candidate HEAD

| | |
|---|---|
| Repository | `Quantum-L9/Enrichment.Inference.Engine` |
| Branch | `claude/eie-domain-closure-86oi52` |
| Base | `origin/main` @ `cfda45043477bfe4a0f2a8c249ff9be30d1705aa` |
| Candidate HEAD | `df065ea30cf9ff9ede790be4797aa31c6b18cc17` |
| Python | 3.12.3 (repo pins 3.12; the container default 3.11 cannot install the SDK) |
| Gate_SDK installed | `1.0.1` @ `a770e8531dc1c59ce01e1dbb0f4162785d9dda89` (unchanged) |
| Migration head | `002` |

## Canonical Converge Contract

One contract: `EnrichRequest` → `EnrichResponse`, carried untranslated.
`handle_converge` no longer discriminates between dialects; it validates,
installs the shared deadline, runs convergence, commits required persistence,
and returns `EnrichResponse.model_dump()`.

Canonical identity is `EnrichRequest.entity["id"]`. `entity["_odoo_entity_id"]`
is read only as a migration-period alias carrying the same value the live
producer already sets on `entity["id"]`; it does not define the contract.

No canonical response carries `status`, `final_fields`, or `writeback`.
(`final_fields` survives only as a local variable inside
`convergence_controller`, assigned to the canonical `fields` key — not an
envelope.)

## Legacy Dialect Consumer Inventory

Searched: `Enrichment.Inference.Engine`, `IB-Odoo_19` (Staging content),
`Constellation.Gate`, `Gate_SDK`.

| Consumer | Class | Evidence |
|---|---|---|
| IB-Odoo_19 `gate_builders.build_converge_request` | **ACTIVE_PRODUCTION — canonical only** | Sole `ConvergeRequest(...)` construction in the repo; emits `entity`/`object_type`/`objective`/`max_variations`/`kb_context` |
| IB-Odoo_19 `tests/test_gate_canonical_contract.py` | TEST_ONLY — asserts absence | `test_request_does_not_emit_entity_snapshot` |
| IB-Odoo_19 `docs/track_b/*`, `docs/handoffs/*` | DOCUMENTATION | Describe the dialect; not callers |
| Constellation.Gate | NONE | No `entity_snapshot` reference outside transport-neutrality tests |
| Gate_SDK | TEST_ONLY — asserts absence | `test_payload_transparency`, `test_domain_neutrality`: the transport must NOT rename `entity` to `entity_snapshot` |
| EIE `odoo_gate_converge.py` + its unit tests | SELF | The adapter and its own coverage |

**No ACTIVE_PRODUCTION consumer. No UNKNOWN consumer.**

## Legacy Dialect Disposition

**DELETED.** `app/services/odoo_gate_converge.py` (363 lines) and
`tests/unit/test_odoo_gate_converge.py` (321 lines) removed, along with the
`_handle_odoo_compat_converge` branch and its dispatcher.

Deletion rather than quarantine, because a discriminator can only narrow a
second protocol, never remove it — and this one had already been widened once
until it claimed every live request.

The failure mode was chosen deliberately: a legacy payload does not validate as
an `EnrichRequest`, so it returns a canonical non-completed `EnrichResponse`
and never reaches the convergence loop. That is a visible "we do not speak
this" rather than a silent contract substitution.
Gated by `test_legacy_dialect_payload_fails_instead_of_being_translated`.

Two canonical constants were relocated out of the deleted module:
`DEFAULT_CONVERGE_TIMEOUT_SECONDS` → `request_deadline.CANONICAL_CONVERGE_BUDGET_SECONDS`,
and `ODOO_COMPLETED_STATE` → `schemas.COMPLETED_STATE` (an Odoo-flavoured name
for a canonical concept).

## Gate_SDK Pin / Capability

**Pin unchanged at `a770e853`. PENDING_EXTERNAL_CANDIDATE.**

The pin is an ancestor of Gate_SDK `main` (`d09fe58`), 17 commits behind. The
intervening diff touches `transport/hashing.py` and `transport/packet.py`
("UTC-stable hash + derive hop reset"), which is transport-relevant. ADR-EIE-016
requires the accepted revision to belong to a coordinated transport release set
with executable cross-repo proof; Gate_SDK closure is a concurrent workstream
and no such release set is established. Bumping on the grounds that `main` is
newer is exactly what that ADR forbids.

## Gate Registration State

**BLOCKED_EXTERNAL_SDK_CAPABILITY. Bespoke registration retained, deliberately.**

- Constellation.Gate `action_ownership.assert_can_claim` refuses a canonical
  action unless the registration carries `metadata.owner`.
- Gate_SDK `gate/registration.build_registration_payload` emits only `version`,
  `type`, `generated_by` — verified against installed `1.0.1` **and** current
  `main`.

Routing EIE registration through the SDK today would have Gate reject
`converge`: the node would start, report healthy, and silently never receive a
packet. `test_sdk_still_cannot_express_owner` asserts the gap and **fails when
the SDK closes it**, which is the signal to delete the local HTTP.

## Persistence Semantics

Required and optional are now different contracts.

| Effect | Contract | On failure |
|---|---|---|
| Enrichment result persistence | REQUIRED on canonical converge | raises `RequiredSideEffectError`; handler returns non-completed |
| Graph synchronisation | excluded from canonical converge | n/a |
| Score invalidation | OPTIONAL | recorded in report + log; result stays `completed` |
| Event emission | OPTIONAL | recorded in report + log; result stays `completed` |

Previously every one of these was caught, logged at WARNING, and the report
discarded by the caller — so a convergence whose result never reached
PostgreSQL was acknowledged to Odoo as durable.

## Tenant Isolation

`UNIQUE (tenant_id, idempotency_key)` replaces the table-wide `UNIQUE
(idempotency_key)`. Every lookup takes `tenant_id` as a first-class argument,
not a post-filter. Proven against real PostgreSQL: the same raw key under two
tenants creates two operations, and a lookup by tenant B never returns tenant
A's row.

## Idempotency Semantics

Key priority is now:

1. `tenant` + caller `idempotency_key` — the only identity that survives a retry
2. `tenant` + `packet_id` — one transport attempt; cannot recognise a replay, never primary
3. `None` — no logical identity ⇒ **no completion dedupe**, effects still run

The retired entity hash (`tenant|entity|action`) made the first enrichment of
an entity permanently mark every later one a duplicate — data loss wearing
idempotency's clothes.

**Open external item:** the live Odoo producer does not set
`idempotency_key`, so canonical converge currently runs in case 3 — correct
(a row per run) but without replay dedupe. Per §7 this is recorded, not
faked: no entity-level exactly-once semantics were invented. Closing it is an
IB-Odoo_19 change (`build_converge_request` already has the field, and
`ConvergeRequest.to_dict` already serialises it), out of scope here.

Note also that `packet_id` never reaches a 2-parameter handler: Gate_SDK
`_invoke_handler` passes `(packet.tenant.org_id, packet.payload)`, so the
transport's own `header.idempotency_key` is not visible to `handle_converge`
today. That is the cleanest future source for case 1.

## Concurrent Replay State

The database is the authority. A lost `(tenant_id, idempotency_key)` race is
caught as `IntegrityError`, the winner is read back, and the caller receives it
as an idempotent hit — a duplicate of an operation that already committed is a
success, not a persistence failure. A per-key `asyncio.Lock` serialises
in-process callers as an optimisation; it is documented as such and is not the
invariant.

## Deadline State — PRESERVED

One monotonic 25 s deadline over the complete operation, 2 s response reserve,
20 s provider attempt cap. Retries consume the same deadline. Unchanged by this
work and still gated.

## Provider Retry State — PRESERVED

EIE is sole retry owner. `Perplexity(api_key=..., max_retries=0)` at client
construction and `max_retries=0` at the call site. Unchanged.

## Provider Transport Timeout Evidence — PRESERVED

The `timeout` argument reaches the real `chat.completions.create()` call and is
derived from remaining budget. Existing gates in
`test_canonical_converge_launch_contract.py` exercise the SDK call boundary
rather than mocking `asyncio.wait_for`. Unchanged.

## Graph State — PRESERVED

Canonical converge makes **zero** synchronous Graph calls (`graph_sync=False`).
Gated by `test_canonical_converge_makes_zero_graph_calls`.

## Writeback Boundary

Canonical converge never mutates Odoo and returns no writeback envelope.
`handle_writeback` remains a separate action; its disposition depends on a
consumer inventory that is not in this task's deletion scope, and it is not
reachable from `converge`.

## Action Registration Matrix

| Action | Implemented | Runtime-allowed | Registered with Gate |
|---|---|---|---|
| `converge` | ✅ | ✅ | ✅ |
| `enrich` | ✅ | ✅ | ✅ |
| `enrich-and-sync` | ✅ (`orchestration_layer`) | ✅ | ✅ |
| `graph-inference-result` | ✅ (`chassis_handlers`) | ✅ | ✅ |
| `enrichbatch`, `discover`, `simulate`, `writeback` | ✅ | ✅ | not advertised |
| `community-export`, `schema-proposal` | ✅ | ✅ | not advertised |

`advertised ⊆ implemented ⊆ runtime_allowed` holds and is gated. Nothing is
advertised that this node cannot serve.

## Real PostgreSQL Evidence

Docker was unavailable in this environment (`/var/run/docker.sock` absent), so
a real PostgreSQL **16.13** server was run directly on port 55432. No mock, no
SQLite, no in-memory substitute.

9 gates in `tests/integration/test_persistence_idempotency_postgres.py`, all
passing, each verified non-vacuous against the pre-fix code:

- reverting the schema to global `UNIQUE` fails the two tenant-isolation gates;
- removing the `IntegrityError` resolution fails the concurrency gate.

Migration `002` proven end to end on real data: `001` → insert 4 rows
(including a cross-tenant key collision that the old constraint **rejected**) →
`002` → all 4 rows intact, cross-tenant reuse now accepted, same-tenant
duplicates still rejected, multiple NULL keys allowed, `002 → 001 → 002`
round-trip clean. The downgrade **refuses loudly** while cross-tenant
duplicates exist rather than deleting a tenant's history to satisfy the
narrower constraint.

## Cross-Repository Alignment

`sdk_gate_style_contract: PASS` — the live Odoo wire payload (structurally
copied from `build_converge_request`) validates as an `EnrichRequest`, routes
to canonical converge, reaches the loop untranslated, and returns a response
whose `state`/`failure_reason`/`fields` are what `map_converge_response` reads.

`real_gate_runtime: NOT_RUN` — no Constellation.Gate instance was exercised.
Stated as not run rather than implied by a passing mock.

## Tests Actually Executed

| Command | Result |
|---|---|
| `pytest tests/` (baseline, pre-change) | 1577 passed, 4 xfailed, 74.32% |
| `pytest tests/` (candidate HEAD, with real PG) | **1584 passed, 4 xfailed, 74.70%** |
| `pytest tests/integration/test_persistence_idempotency_postgres.py` | 9 passed |
| Same, against pre-fix schema | 2 failed (non-vacuous) |
| Same, without IntegrityError resolution | 1 failed (non-vacuous) |
| `pytest tests/unit/test_durable_before_completed.py` | 10 passed |
| `pytest tests/contracts/test_gate_registration_boundary.py` | 4 passed |
| `ruff check` / `ruff format --check` | clean |
| `alembic upgrade 001 → 002`, `downgrade`, round-trip | proven on real PG 16.13 |

## Remaining Blocking Defects

None.

## Remaining Non-Blocking Defects

1. **Redis `IdempotencyStore` is not tenant-scoped** — `enrich:idem:{key}` with
   no tenant prefix, the same family of defect as the database one just fixed.
   It is not on the canonical converge path (the convergence loop does not
   consult it), so it is reported rather than changed inside this pass.
2. **`packet_id` is unreachable from `handle_converge`** — the SDK passes only
   `(tenant, payload)` to a 2-parameter handler, so the `pkt:` fallback is
   currently dead on this path. Harmless, and the right fix is case 1 below.

## External Blockers

| Item | Status | Unblocks when |
|---|---|---|
| Gate registration → SDK | BLOCKED_EXTERNAL_SDK_CAPABILITY | Gate_SDK `build_registration_payload` emits `metadata.owner` |
| Gate_SDK pin bump | PENDING_EXTERNAL_CANDIDATE | a coordinated transport release set exists with cross-repo proof |
| Odoo supplies `idempotency_key` | EXTERNAL (IB-Odoo_19) | `build_converge_request` sets the field it already carries |

## Deferred Work

Graph redesign; generalised provider abstraction; provider waterfall;
queue/outbox; unrelated CRM refactors; generalised action framework. None was
needed to satisfy an invariant here.

## Scope Drift Audit

No new queue, outbox, scheduler, provider framework, state machine, transport,
routing system, or distributed lock was introduced. The fixes use the existing
PostgreSQL, the existing domain models, the existing Gate_SDK, and the existing
deadline/retry mechanism.

Three fixes lie outside the literal task list and are justified as
in-scope-on-identification (rule 42): the alembic no-op (without it, migration
002 could not be proven — or trusted in production), the duplicate index
(blocks schema creation against a real server), and the migration's
`name[]`/`text[]` cast (found by running it).

## Merge Recommendation

**APPROVE.** Every claimed invariant has an executable gate; the two open items
are external and tripwired rather than described.

## Release-Set Recommendation

**PENDING.** EIE is ready on its own terms. Joining a transport release set
requires the Gate_SDK candidate and a real-Gate round trip, neither available
here.

## Next Straight-Line Move

Land the Gate_SDK `metadata.owner` capability. It is the single change that
unblocks registration migration, and `test_sdk_still_cannot_express_owner`
turns red the moment it lands, so the follow-up in EIE is a deletion with an
automatic trigger rather than a remembered chore.

## Machine-Readable Summary

```yaml
repository: Quantum-L9/Enrichment.Inference.Engine
branch: claude/eie-domain-closure-86oi52
candidate_head: "df065ea30cf9ff9ede790be4797aa31c6b18cc17"
canonical:
  action: converge
  request: EnrichRequest
  response: EnrichResponse
  alternate_dialect_present: false
  consumer_inventory_complete: true
persistence:
  required_before_completed: true
  failure_propagates: true
  completion_on_failure: false
  real_postgres_proven: true
idempotency:
  logical_identity_primary: true
  packet_id_primary: false
  tenant_scoped_database_key: true
  entity_only_completion_fallback: false
  concurrent_same_key_safe: true
deadline:
  complete_operation_seconds: 25
  shared_monotonic: true
provider:
  retry_owner: EIE
  sdk_auto_retries: 0
  actual_network_timeout: PASS
  blocking_io_off_event_loop: true
graph:
  canonical_sync_calls: 0
gate_sdk:
  pinned_sha: "a770e8531dc1c59ce01e1dbb0f4162785d9dda89"
  installed_package: PASS
  runtime_compatible: PASS
registration:
  sdk_owned: false
  manual_http_remaining: true
  manual_http_reason: "Gate_SDK build_registration_payload emits no metadata.owner; Gate requires it for canonical actions"
  gate_registered_readiness: PASS
writeback:
  canonical_converge_mutates_odoo: false
validation:
  unit: PASS
  contracts: PASS
  integration: PASS
  real_postgres: PASS
  make_pr: SEE_PR_FINDINGS_BRIEF
blocking_defects: []
non_blocking_defects:
  - "Redis IdempotencyStore key is not tenant-scoped (off the canonical converge path)"
  - "packet_id is unreachable from a 2-parameter SDK handler, so the pkt: fallback is dead on converge"
external_blockers:
  - "Gate_SDK cannot express metadata.owner — registration migration blocked"
  - "No coordinated transport release set — Gate_SDK pin bump pending"
  - "IB-Odoo_19 does not set idempotency_key — canonical converge runs without replay dedupe"
verdict:
  local: GO
  contract: GO
  runtime: PROOF_PENDING
  merge: APPROVE
  release_set: PENDING
next_move: "Land metadata.owner in Gate_SDK; the EIE tripwire turns red and the bespoke registration HTTP is deleted."
```
