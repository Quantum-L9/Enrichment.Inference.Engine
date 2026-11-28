# Data Contract: `graph_return_targets`

> CEG feedback (`graph-inference-result`) retained until a later converge
> consumes it (campaign EIE-CEG-GATE-COLLAB, slice A4).

| | |
|---|---|
| Table | `graph_return_targets` |
| Model | `GraphReturnTarget` — `app/services/pg_store.py` |
| Migration | `migrations/versions/006_graph_return_targets.py` (revision `006`, after `005`) |
| Writer | `GraphReturnChannel.submit` → `pg_store.retain_graph_return_targets` (one transaction per packet) |
| Consumer | `run_convergence_loop` → `GraphReturnChannel.claim_for_entity` / `settle` → `pg_store.claim_graph_return_targets` / `settle_graph_return_targets` |
| Ingress | Gate only: SDK runtime `graph-inference-result` → `handle_graph_inference_result` |
| Visibility | Internal. No TransportPacket field, payload key, Gate action or domain-spec key carries any column. The packet stays `inference_outputs`; the answer keeps `status`, `targets_queued`, `packet_id`, `tenant`. |
| Contract test | `tests/contracts/tier2/test_graph_return_intake_contract.py` |

## Why it exists

The return channel kept targets in a per-process `asyncio.Queue`: a restart
lost them, a second worker never saw them, a full queue dropped them while the
packet was still answered `accepted`, and `targets_queued` counted queueing,
not consumption. Where a durable store backs the process, every output is now
recorded before the packet is answered, and a converge records what it did
with each target it used.

Without a store (`pg_store` engine not initialised) the per-process queue is
the fallback. It is best effort, and the single-replica, single-worker
deployment pins it (`tests/unit/test_worker_singleton_invariant.py`,
`tests/unit/test_deployment_topology.py`). Even there, a packet is never
half-accepted and a retransmit is queued once.

## Columns

| Column | Type | Meaning |
|---|---|---|
| `id` | UUID PK | Row identity; a leased target carries it as `EnrichmentTarget.intake_id`. |
| `seq` | BIGINT identity, unique | Arrival order. |
| `tenant_id` | VARCHAR(128) | The Gate packet's tenant (`packet.tenant.org_id`); never an output's own claim. |
| `entity_id` | VARCHAR(256) | Entity the output names. |
| `field_name` | VARCHAR(128) | Field the output names. |
| `rule` | VARCHAR(128) | CEG rule that produced it (`unknown` when absent). Provenance only. |
| `value` | JSONB | The seed value, as `{"value": <v>}`. |
| `value_digest` | VARCHAR(64) | SHA-256 of the value's canonical JSON. |
| `fact_key` | VARCHAR(64) | SHA-256 of tenant, entity, field, rule and value digest: the fact, whoever sent it. |
| `intake_key` | VARCHAR(64) | SHA-256 of the producer key and the fact (A1 F-A1-07). |
| `producer_key` | VARCHAR(256) | The packet header's `idempotency_key`, else `packet:<packet_id>`. |
| `origin_packet_id` | VARCHAR(128) | Packet that delivered it. |
| `confidence` | FLOAT | The output's confidence in [0, 1]. |
| `state` | VARCHAR(32) | See *States*. |
| `reason` | TEXT NULL | Why the row was rejected or expired. |
| `outcome` | VARCHAR(64) NULL | What the consuming converge did with it (see *Outcomes*). |
| `attempts` / `max_attempts` | INTEGER | Leases so far; the ceiling (default 5). |
| `lease_owner` / `lease_expires_at` | VARCHAR(128) / TIMESTAMPTZ NULL | Fencing token of the current lease (`<owner>#<token>`, new on every claim) and its expiry. |
| `expires_at` | TIMESTAMPTZ | TTL: a target no converge reached by then expires. |
| `consumed_by` | VARCHAR(512) NULL | The converge that settled it (`converge:<idempotency key>`, else `converge:<tenant>:<entity>`). |
| `created_at`, `updated_at`, `resolved_at` | TIMESTAMPTZ | Lifecycle timestamps; `resolved_at` set on a terminal state. |

Constraints: `uq_graph_return_targets_tenant_intake` UNIQUE (`tenant_id`, `intake_key`).
Indexes: `ix_graph_return_targets_entity` (`tenant_id`, `entity_id`, `state`, `seq`),
`ix_graph_return_targets_fact` (`tenant_id`, `fact_key`).

## States

```text
intake ──eligible──────────────────────────▶ retained
intake ──ineligible / unchanged fact────────▶ rejected
retained ──converge claim──▶ consuming ──settled──▶ consumed (outcome)
                                │      └──refused──▶ rejected (field_not_in_schema | type_mismatch)
                                └──lease expired──▶ claimable again
retained/consuming ──TTL passed────────────▶ expired (ttl_expired)
retained/consuming ──attempts spent────────▶ expired (attempts_exhausted)
```

| State | Terminal | Meaning |
|---|---|---|
| `retained` | no | Accepted and durable; waiting for a converge of its tenant and entity. |
| `consuming` | no | Leased to one converge until `lease_expires_at`; an expired lease is claimable again. |
| `consumed` | yes | A converge used it; `outcome` says how. |
| `rejected` | yes | Refused at intake or by the converge; `reason` says why. Never consumed. |
| `expired` | yes | No converge settled it within its TTL or attempt ceiling. |

## Rules

1. **Gate-only ingress, packet tenant.** The SDK runtime validates the packet
   first. The tenant is the packet's; an output carrying another `tenant_id`
   or `tenant` is `rejected` (`cross_tenant`), never re-homed.
2. **Refuse per output, never the packet.** A non-map output, a missing or
   non-scalar entity or field, or a confidence that is not a number in [0, 1]
   is `malformed` (logged; not recorded, having no identity). An output below
   `CONFIDENCE_FLOOR` (0.55) is recorded `rejected` (`below_threshold`).
3. **Atomic, idempotent acceptance.** One transaction per packet, under a
   per-tenant advisory lock. An output whose `intake_key` is already recorded
   is a duplicate (Gate retransmit, producer retry, replay after a restart)
   and records nothing; the answer's `targets_queued` counts the packet's
   accepted targets, so a retransmit is answered the same.
4. **Unchanged facts are not new observations.** An eligible output whose
   `fact_key` an accepted row (`retained`, `consuming`, `consumed`) already
   holds with at least its confidence is `rejected` (`unchanged_fact`): an
   echo reseeds nothing and upgrades no confidence. The same fact at a higher
   confidence, or a changed value, is a new observation and is retained.
5. **Never half-accepted.** When retaining a packet's targets would take the
   tenant's open (`retained` + `consuming`) rows past
   `GRAPH_RETURN_TENANT_CAPACITY` (10 000), nothing is recorded and
   `GraphReturnCapacityError` is raised: under the SDK runtime's error
   semantics an internal error (HTTP 500 / failure packet), which the producer
   retries. The in-process fallback refuses the same way when its queue cannot
   take every new target.
6. **Leased consumption.** A converge of tenant T and entity E leases only
   T/E's rows, `FOR UPDATE SKIP LOCKED`, under a fresh fencing token: two
   workers never lease one row, and only that lease settles it. A converge that
   dies leaves its rows to lease expiry (300 s); each lease counts an attempt.
7. **Bounded wait.** A target waits only for the next authorized converge of
   its entity. It never starts a converge, a sweep or a provider call, and
   never spends budget. Past its TTL (7 days) it is `expired`
   (`ttl_expired`); leased `max_attempts` times without being settled, it is
   `expired` (`attempts_exhausted`).
8. **Consumption is evidence, not writeback.** A seeded value enters the pass
   as a graph inference (`graph_inference` source), never provider evidence, a
   verified field, an attestation or a consumer writeback. It never replaces a
   value the request's entity supplied, and a stronger value the loop holds
   wins. A target for a field the tenant spec does not declare is `rejected`
   (`field_not_in_schema`); with no declared spec, no field is refused. A
   target whose value violates its field's declaration (the shared grammar,
   `app/engines/spec_types.value_defects`) is `rejected` (`type_mismatch`): it
   is never seeded and never displaces a value. With a bound node only that
   node's declaration applies; with none, every declaration of the name does
   (EIE-PR-U01).

## Outcomes

| Outcome | Meaning |
|---|---|
| `seeded` | The target's value filled the field for the pass. |
| `outranked` | It was seeded, then a stronger target for the same field replaced it. |
| `kept_entity_value` | The request's entity supplied the field; the record's own data wins. |
| `kept_stronger_value` | The loop already held the field at equal or higher confidence. |

`accepted` (intake answer) ≠ `retained` (row state) ≠ `consumed` (a converge
used it) ≠ improved: whether the converge's result changed is the converge's
own evidence (`graph_seeded` per pass, the response's field evidence).

## Operator view

`pg_store.graph_return_status_counts(tenant_id=None)` returns the count per
state; `GraphReturnChannel.stats()` reports accepted, duplicates, rejected,
refused packets and whether the store is durable. Structured logs:
`graph_return_targets_recorded`, `graph_return_outputs_rejected`,
`graph_return_targets_claimed`, `graph_return_targets_settled`,
`graph_return_claim_failed`, `graph_return_settle_failed`.

## Not covered

- A converge that consumed targets and then failed to persist its result has
  still consumed them; the targets are not re-offered.
- The in-process fallback does not survive a restart and is not shared between
  processes; the deployment pins one process.
- No rule allow-list exists in the tenant spec; `rule` is provenance only. An
  allow-list would be a new domain-spec key.
