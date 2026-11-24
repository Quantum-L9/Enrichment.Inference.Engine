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
| `contract_digest` | VARCHAR(64) NULL | SHA-256 of the endpoint contract (entity type, id property, target node, typed row schema) the fields were admitted against. NULL when no contract resolved at commit; such a row is refused at dispatch (`refused:unpinned`), never sent. |
| `cartridge_revision` | VARCHAR(128) NULL | Reserved for the effective spec revision; not populated by A2. |
| `origin` | VARCHAR(32) | Workflow the obligation belongs to: `enrich` or `converge`. Recovery holds `converge` rows while `CONVERGE_GRAPH_SYNC_ENABLED` is off. |
| `fields` | JSONB | Exactly the typed fields the leg sends (see *Field set and provenance*): in full mode each with its provenance property, in legacy mode the admitted values unlabelled; a `list<map>` stays a list of maps. |
| `enrichment_result_id` | UUID NULL | The `enrichment_results` row this obligation follows. |
| `state` | VARCHAR(32) | See *States*. |
| `attempts` / `max_attempts` | INTEGER | Claims so far; the ceiling (default 8). |
| `next_attempt_at` | TIMESTAMPTZ | Earliest next claim (backoff). |
| `lease_owner` / `lease_expires_at` | VARCHAR(128) / TIMESTAMPTZ NULL | Fencing token of the current claim (`<owner>#<token>`, new on every claim) and its lease. |
| `last_error` / `last_ack` | TEXT / JSONB NULL | Last failure reason; CEG's acknowledgment when acknowledged. |
| `created_at`, `updated_at`, `resolved_at` | TIMESTAMPTZ | Lifecycle timestamps; `resolved_at` set on a terminal state. |

Constraints: `uq_graph_sync_intents_tenant_key` UNIQUE (`tenant_id`, `effect_key`).
Indexes: `ix_graph_sync_intents_due` (`state`, `next_attempt_at`),
`ix_graph_sync_intents_subject` (`tenant_id`, `entity_id`, `endpoint`, `seq`).

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
   for its `tenant_id` / `entity_id` / `node_label`. The Gate idempotency key
   binds the row **and** the operation (`effect_key`): retries of one version
   reuse it; two versions producing the same row never share it, so Gate's
   cache cannot acknowledge a later version with an earlier one's answer. Past
   Gate's cache TTL a replay lands as CEG's `MERGE` on the same id.
3. **Pinned contract.** The digest covers the endpoint's entity type, id
   property, target node and typed row schema. When it differs from the
   tenant's current contract nothing is sent (`refused:contract_changed`); a
   row with no digest is never sent (`refused:unpinned`).
4. **Leases and fencing.** A claim is `FOR UPDATE SKIP LOCKED` and stamps a new
   fencing token; only that claim settles the row. A claim whose lease expired
   and was re-claimed -- by another worker or the same process -- settles
   nothing. Recovery claims one row at a time, immediately before sending it.
   Each claim is its own transaction holding a per-subject advisory lock
   (`pg_advisory_xact_lock`) until it commits, so two claims of one subject
   are serialised and the later one sees the earlier one's lease.
5. **Version order.** A subject is the graph row written: tenant, entity and
   resolved endpoint. A row is not claimed while an older row of its subject is
   `pending` or `dispatching`, nor once a newer row was **sent** (claimed at
   least once, in any state since); such an older `pending` row is
   `superseded`. Version order governs which row EIE claims and sends; it
   cannot stop CEG from applying an earlier request that timed out in transit
   after a later one was acknowledged. The echo guard (G4, *Field set and
   provenance*) therefore never treats an acknowledgment as proof of the graph
   state while such an earlier outcome is unknown.
6. **Bounded retries.** Retryable outcomes back off 2 s doubling to 300 s; at
   `max_attempts` the row is `failed_terminal` with `retry_exhausted:*`.
7. **Ownership before sending.** A store that cannot confirm a claim sends
   nothing for the key; recovery sends it. Only a process with no durable store
   configured sends unclaimed. A CEG answer the row did not record (lease lost,
   store error) leaves the key pending.
8. **Verified replay.** An idempotent replay attaches its intent to the stored
   result only when it carries the same entity and committed fields and every
   graph field it pins equals the stored result's value for that field (a
   shaped or absent graph field cannot be verified). A replay of an operation
   whose intent already exists is its duplicate, checked first. A replay
   carrying anything else raises `GraphIntentReplayMismatchError`: it is a
   failed persistence for that call (refused when durability is required),
   never a duplicate and never `completed`.
9. **Recovery.** `SideEffectCoordinator.recover_graph_intents` claims due rows
   and replays them; startup registration runs it every 30 s in-process, so a
   restarted idle node recovers without traffic.

## Field set and provenance

Operator ruling 2026-11-24 (revision of the #351 rule "observed / request values
never reach the graph"): **a converge graph sync carries both admitted
(enriched) values and consumer-observed values** for every field the effective
spec declares on the resolved sync endpoint's row contract, childsync
`list<map>` fields included. Before this ruling `fields` held admitted values
only. Response admission is unchanged: an observed value is still never
admitted into a response's `fields` (`docs/contracts/converge/response.v2.md`
§3). This section governs only what the graph leg sends.

Each declared row field `F` syncs in one of two modes, decided by whether the
endpoint declares `F`'s provenance property (`F_provenance`, or the item
`provenance` of a childsync `list<map>`). CEG packs are published by
operators independently of EIE releases, so an endpoint that has not declared
provenance keeps today's behaviour exactly:

| Mode | When | What syncs for `F` |
|---|---|---|
| **Legacy** | `F`'s provenance property is not declared | Only the admitted (enriched) value, exactly as before this ruling (`handlers.admitted_fields`), with no provenance property. Unlabelled means enriched by contract. G2–G4 do not apply; G5 holds as before. |
| **Full** | `F`'s provenance property is declared | The observed or the enriched value, each with its provenance property, under G1–G6 below. |

An observed value **never** syncs to a field whose provenance property is not
declared. The guardrails of full mode are applied per declared row field `F`,
in this order:

| Guardrail | Rule |
|---|---|
| **G1 Provenance** | Every field sent in full mode carries its provenance, `observed` or `enriched`, as a **declared spec property**, by the convention of `docs/contracts/EIE_NATIVE_DOMAIN_SPEC.md` *Sync provenance*: a row field `F` is accompanied by its companion property `F_provenance`; every item of a childsync `list<map>` carries the item property `provenance`. An observed value is never labelled `enriched`, and never sent unlabelled. |
| **G2 Precedence within one converge** | Candidates first exclude any value that originated from a CEG `graph-inference-result` (evidence source type `graph`; see G4). Of the rest, an admitted value (`inferred` or `external_claim`, `admitted: true`) is sent as `enriched` and wins over an observed value of the same field. Otherwise the consumer's observed value for `F` (the request entity, or a v2 observation) is sent as `observed` -- including when the only admitted value was graph-origin and so excluded. A conflicting set of observations sends nothing for `F`. |
| **G3 Precedence over time** | Enforced by CEG, not EIE: in the graph a later `observed` value replaces an `enriched` one, and an `enriched` value never overwrites an `observed` one. An enriched value CEG displaces is not lost: it remains EIE evidence (`enrichment_results`, `field_evidence`). |
| **G4 Echo guard** | `F` is not sent only when both hold: (1) the subject's (tenant, entity, endpoint) most recent intent carrying `F` is `acknowledged` with the same value and provenance, and (2) every intent of the subject carrying `F` that was ever sent (claimed at least once) ended `acknowledged`. An acknowledgment is not proof of the graph state while any sent intent's outcome is unknown: an intent that was sent and ended `failed_terminal` or `superseded` may still be applied by CEG later (Gate: after a transport failure the graph "may or may not hold the row"), even after a newer acknowledgment, so `F` is then always resent to reconcile. `F` is always sent while an intent carrying `F` is `pending` or `dispatching`. A value that originated from a CEG `graph-inference-result` (evidence source type `graph`) is never sent (excluded before G2). A field set left empty sends no row and records no intent. |
| **G5 No clears** | A `null` or missing observed value is never sent, so it never deletes a graph property; the same holds for admitted values. |
| **G6 Domain-agnostic** | The field set is keyed only on the endpoint's declared row properties and its `childsync` entries. No field name, node label or cartridge is known to the code that builds it. |

Rule 8 (*Verified replay*) is unchanged: a replay is verified against the
stored result's committed `fields`. The stored result holds admitted values
only, so a pinned observed field or provenance property is absent from it and
cannot be verified: such a replay is a `GraphIntentReplayMismatchError` and
attaches no intent (fails closed).

## Outcome classification (`PacketRouter.notify_graph_sync(on_outcome=…)`)

| Outcome | Settles as | Cause |
|---|---|---|
| ACKNOWLEDGED | `acknowledged` | GRAPH_ACKNOWLEDGED: `status == "success"`, `entity_type` is the endpoint, `synced_count == len(batch)`, `rejected_count` absent or 0 (A1 F-A1-01). A node ack carries no created/replayed counts. |
| REFUSED | `failed_terminal` | `no_contract`, `contract_changed`, `unpinned`, `row_type` (value off its declared type; nothing sent), `all_rejected` naming this endpoint with `synced_count == 0`, `rows_rejected` (a success answer for this endpoint with `rejected_count` > 0, F-A1-01), or a Gate 4xx. |
| RETRYABLE | `pending` (backoff) | Gate unreachable, timeout or 5xx; a worker `failure` packet; an answer that does not confirm this row. |

## Operator view

`pg_store.graph_intent_status_counts(tenant_id=None)` returns the count per
state. Structured logs: `graph_intent_recorded`, `graph_intents_claimed`,
`graph_intent_settled`, `side_effects_graph_retry_exhausted`.

## Not covered

- The completion event is not a durable obligation: a process that stops
  after the result commits and before the event is emitted does not re-emit it
  (the next commit of the operation is a duplicate).

- Relationship evidence sync (`notify_relationship_sync`) is not backed by this
  table.
- The response packet's tenant is not checked by the Gate SDK client; an ack
  carries no tenant of its own.
