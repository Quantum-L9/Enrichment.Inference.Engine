# Data Contract: `graph_sync_intents`

> The durable graph obligation of a committed enrichment result
> (campaign EIE-CEG-GATE-COLLAB, slice A2).

| | |
|---|---|
| Table | `graph_sync_intents` |
| Model | `GraphSyncIntent` — `app/services/pg_store.py` |
| Migration | `migrations/versions/005_graph_sync_intents.py` (revision `005`, after `004`) |
| Writer | `pg_store.save_enrichment_result` inside `pg_store.graph_intent_scope(spec)`, called by `SideEffectCoordinator.commit_after_enrich` |
| Dispatcher | `SideEffectCoordinator._graph_leg` / `recover_graph_intents` → `PacketRouter.notify_graph_sync` → Gate `sync` → CEG |
| Visibility | Internal. No TransportPacket field, payload key, Gate action or domain-spec key carries any column. |
| Contract test | `tests/contracts/tier2/test_graph_sync_intent_contract.py` |

## Why it exists

A result EIE answers `completed` has a graph leg that is deferred past the
caller's deadline (`converge`) or may fail (CEG or Gate unavailable, ack lost).
Before this table the obligation lived only in a process-local dict, so a
restart lost it while the producer had already been told `completed`. The row
is written **in the result's own transaction**, so a committed result with its
graph leg requested always has its obligation, and a result that did not commit
leaves none.

## Columns

| Column | Type | Meaning |
|---|---|---|
| `id` | UUID PK | Row identity. |
| `seq` | BIGINT identity, unique | Insertion order; orders versions of one subject. |
| `tenant_id` | VARCHAR(128) | Tenant whose result this follows; must equal the result's tenant. |
| `effect_key` | VARCHAR(512) | Semantic operation key (`semantic_side_effect_key`): the caller's idempotency key, else the transport packet id, else the result digest. |
| `entity_id` | VARCHAR(256) | Entity the pinned fields were persisted for. |
| `node_label` | VARCHAR(128) NULL | Ontology node the endpoint is selected by. |
| `domain` | VARCHAR(128) NULL | Effective cartridge id at commit. |
| `endpoint` | VARCHAR(128) NULL | CEG sync endpoint (`entity_type`) resolved at commit, when one resolved. |
| `contract_digest` | VARCHAR(64) NULL | SHA-256 of the endpoint contract (entity type, id property, typed row schema) the fields were admitted against. |
| `cartridge_revision` | VARCHAR(128) NULL | Reserved for the effective spec revision; not populated by A2. |
| `fields` | JSONB | Exactly the admitted typed fields the leg sends (a `list<map>` stays a list of maps). |
| `enrichment_result_id` | UUID NULL | The `enrichment_results` row this obligation follows. |
| `state` | VARCHAR(32) | See *States*. |
| `attempts` / `max_attempts` | INTEGER | Claims so far; the ceiling (default 8). |
| `next_attempt_at` | TIMESTAMPTZ | Earliest next claim (backoff). |
| `lease_owner` / `lease_expires_at` | VARCHAR(128) / TIMESTAMPTZ NULL | Current claim holder and its lease. |
| `last_error` / `last_ack` | TEXT / JSONB NULL | Last failure reason; CEG's acknowledgment when acknowledged. |
| `created_at`, `updated_at`, `resolved_at` | TIMESTAMPTZ | Lifecycle timestamps; `resolved_at` set on a terminal state. |

Constraints: `uq_graph_sync_intents_tenant_key` UNIQUE (`tenant_id`, `effect_key`).
Indexes: `ix_graph_sync_intents_due` (`state`, `next_attempt_at`),
`ix_graph_sync_intents_subject` (`tenant_id`, `entity_id`, `node_label`, `seq`).

## States

```text
pending ──claim──▶ dispatching ──ack──────────────▶ acknowledged
   ▲                   │ ├──refused───────────────▶ failed_terminal
   └──retry (backoff)──┘ └──retry, attempts spent─▶ failed_terminal (retry_exhausted)
pending ──newer version of the subject sent──────▶ superseded
dispatching, lease expired, attempts spent ──────▶ failed_terminal (retry_exhausted:lease_expired)
```

| State | Terminal | Meaning |
|---|---|---|
| `pending` | no | Due at `next_attempt_at`. |
| `dispatching` | no | Leased to `lease_owner` until `lease_expires_at`; an expired lease is claimable again. |
| `acknowledged` | yes | CEG confirmed the row (GRAPH_ACKNOWLEDGED, below). |
| `failed_terminal` | yes | Refused, or retries spent. Never reported as synced. |
| `superseded` | yes | A newer version of the same subject was already sent or acknowledged. |

## Rules

1. **Atomic with the result.** The intent is inserted before the result row in
   the same transaction. A second commit of the same operation — another
   worker, or this process after a restart — waits on the intent key, inserts
   nothing, raises `GraphIntentDuplicateError`, and rolls back whole: one
   result, one completion event, one obligation.
2. **Pinned input.** Every attempt and every replay sends the row's `fields`
   for its `tenant_id` / `entity_id` / `node_label`. A replay therefore
   produces the same sync row and the same Gate idempotency key
   (`eie:sync:<tenant>:<entity>:<row digest>`); past Gate's cache TTL it lands
   as CEG's `MERGE` on the same id.
3. **Pinned contract.** When `contract_digest` is set and the tenant's current
   endpoint contract differs, nothing is sent and the row ends
   `failed_terminal` (`refused:contract_changed`).
4. **Leases.** A claim is `FOR UPDATE SKIP LOCKED`: one row is leased to one
   worker. Only the lease holder settles; a holder whose lease expired and was
   re-claimed settles nothing.
5. **Version order.** A row is not claimed while an older row of the same
   subject is `pending` or `dispatching`; a `pending` row whose subject has a
   newer row `acknowledged` or `dispatching` becomes `superseded`.
6. **Bounded retries.** Retryable outcomes back off 2 s doubling to 300 s; at
   `max_attempts` the row is `failed_terminal` with `retry_exhausted:*`.
7. **Recovery.** `SideEffectCoordinator.recover_graph_intents` claims due rows
   and replays them; startup registration runs it every 30 s in-process, so a
   restarted idle node recovers without traffic.

## Outcome classification (`PacketRouter.notify_graph_sync(on_outcome=…)`)

| Outcome | Settles as | Cause |
|---|---|---|
| ACKNOWLEDGED | `acknowledged` | GRAPH_ACKNOWLEDGED: `status == "success"`, `entity_type` is the endpoint, `synced_count == len(batch)`, `rejected_count` absent or 0 (A1 F-A1-01). A node ack carries no created/replayed counts. |
| REFUSED | `failed_terminal` | `no_contract`, `contract_changed`, `row_type` (value off its declared type; nothing sent), `all_rejected`, or a Gate 4xx. |
| RETRYABLE | `pending` (backoff) | Gate unreachable, timeout or 5xx; a worker `failure` packet; an answer that does not confirm this row. |

## Operator view

`pg_store.graph_intent_status_counts(tenant_id=None)` returns the count per
state. Structured logs: `graph_intent_recorded`, `graph_intents_claimed`,
`graph_intent_settled`, `side_effects_graph_retry_exhausted`.

## Not covered

- Relationship evidence sync (`notify_relationship_sync`) is not backed by this
  table.
- The response packet's tenant is not checked by the Gate SDK client; an ack
  carries no tenant of its own.
