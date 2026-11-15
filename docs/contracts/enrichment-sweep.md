# Enrichment sweep

L9_META:
  tier: 2
  domain: convergence
  authority: CTO lane eie-enrichment-sweep, operator decisions 2026-11-14/15

## 1. Scope

Two SDK actions, carried as TransportPacket payloads and routed by Gate:

| Action | Handler | Purpose |
| --- | --- | --- |
| `enrichment-sweep` | `app/engines/sweep_handlers.py` `handle_enrichment_sweep` | Accept candidate records and a daily budget; enrich the ones the graph engine reports gaps for, within budget, in the background. |
| `enrichment-sweep-results` | `app/engines/sweep_handlers.py` `handle_enrichment_sweep_results` | Page what a sweep produced, its budget and its skips. |

EIE owns the trigger: it decides which records to enrich and when, from the
readiness gaps the graph engine (CEG) reports. The consumer owns the
selection of candidates and requests the daily budget. Every result is
reviewed by a human in the consumer before anything is written there. EIE
writes nothing to the consumer. Converge already sends its results to CEG.

The tenant is the one the chassis resolved from the packet. Node labels and
field names come from the tenant's cartridge (tenant id == cartridge id) or
from CEG's answer. Shared code names no tenant, domain, node or field.

Both actions are registered with the SDK runtime, listed in
`app.main.RUNTIME_ALLOWED_ACTIONS` and advertised in `app.main.ADVERTISED_ACTIONS`
(advertised ⊆ implemented ⊆ runtime_allowed). Both refuse with `disabled` while
`L9_ENRICHMENT_SWEEP_ENABLED` is false (the default). Gate routing for both
actions, and Gate permission for EIE to call `health-assess`, are not in this
repository (Gate #43).

### 1.1 Payload field names (exact)

| Action | Top-level members | Required |
| --- | --- | --- |
| `enrichment-sweep` | `candidates`, `daily_budget_usd`, `idempotency_key` | all three |
| `enrichment-sweep` candidate | `entity_id`, `entity`, `node_label`, `field_confidences` | `entity_id`, `entity` |
| `enrichment-sweep-results` | `sweep_id`, `cursor` | `sweep_id` |

Any other member is refused (`invalid_request` for the envelope, `invalid_candidate`
for a candidate). `enrichment-sweep-results` takes no `idempotency_key`.

## 2. Settings

| Setting (env) | Default | Meaning |
| --- | --- | --- |
| `L9_ENRICHMENT_SWEEP_ENABLED` | `false` | Feature flag. |
| `L9_SWEEP_MAX_DAILY_BUDGET_USD` | `5.0` | Deployment ceiling on any requested daily budget. |
| `L9_SWEEP_MAX_CANDIDATES_PER_REQUEST` | `500` | Candidates accepted per request; the rest are rejected. |
| `L9_SWEEP_RESULT_TTL_HOURS` | `72` | How long a sweep is kept after it completes. |
| `L9_SWEEP_RESULTS_PAGE_SIZE` | `50` | Results per page. |

## 3. `enrichment-sweep`

### 3.1 Request

```json
{
  "candidates": [
    {
      "entity_id": "<consumer record id>",
      "node_label": "<optional: a node the tenant's cartridge declares>",
      "entity": {"...": "the record's current values"},
      "field_confidences": {"<field>": 0.7}
    }
  ],
  "daily_budget_usd": 2.5,
  "idempotency_key": "<consumer key, unique per sweep>"
}
```

* `node_label` may be omitted only when the cartridge declares exactly one node.
* `field_confidences` is optional; values are numbers in [0, 1].
* Unknown members are refused.

### 3.2 Response (immediate)

```json
{"sweep_id": "<32 hex>", "accepted": ["<entity_id>"], "rejected": [{"entity_id": "...", "reason": "..."}]}
```

### 3.3 Idempotency

`enrichment-sweep` is idempotent on the top-level string `idempotency_key`,
per tenant (the chassis-resolved tenant, never a payload field):

* The key → `sweep_id` mapping is persisted (`enrichment_sweeps`, unique on
  `(tenant_id, idempotency_key)`), including for a sweep in which no candidate
  was accepted (recorded already `completed`).
* A replay with the same key returns the original `{sweep_id, accepted,
  rejected}` exactly, whatever the replayed `candidates` or `daily_budget_usd`
  say. It starts no new work, re-assesses nothing, and reserves no budget.
* `sweep_id` is deterministic per (tenant, `idempotency_key`).
* The envelope is validated before the lookup: a replay whose envelope is
  invalid is refused `invalid_request`, as the original would have been.
* The mapping lives as long as the sweep (section 6.3). A key reused after its
  sweep has been deleted starts a new sweep.

Rejection reasons: `invalid_candidate`, `invalid_entity_id`, `invalid_entity`,
`invalid_field_confidences`, `node_label_required`, `node_not_declared`,
`duplicate_entity_id`, `candidate_cap_exceeded`.

### 3.4 The background run

1. **Readiness.** Accepted candidates are sent to CEG through Gate, action
   `health-assess`, in batches of at most 100:
   `{subaction: "health_batch_assess", entities: [{entity_id, node_label, data,
   field_confidences}], max_entities: <batch size>}`. The record goes in
   `data`. `domain` and `tier` are not sent. The call uses the one EIE Gate
   client (`PacketRouter.route`), never a peer URL.
2. **What is read.** Only `entities[i].entity_health.{critical_gaps,
   enrichment_targets, readiness_score, grade, gate_completeness}`, in CEG's
   shapes (engine/health/field_health.py): `critical_gaps` is a list of
   field-name strings; `enrichment_targets` is a list of EnrichmentTarget
   objects, of which only `field_name` is read. Any other shape skips the
   candidate as malformed. A successful batch answer carries no `status`
   (CEG's BatchHealthReport has none); only `{"status": "disabled"}` and other
   non-answer statuses are refusals. Every other field of
   the reply (`enrichment_priority`, `enrichment_dispatch`, `upgrade_prompt`,
   tier fields) is ignored.
3. **Ranking.** Candidates with critical gaps or enrichment targets, ordered
   by: critical gaps first; then CEG's order within its batch reply; then lower
   `readiness_score` (missing last). Candidates without gaps are skipped
   (`no_readiness_gaps`).
4. **Targets.** CEG's `critical_gaps` then `enrichment_targets`, deduplicated.
   Names the cartridge does not declare on the candidate's node are dropped and
   counted (`stats.targets_dropped_undeclared`). A candidate left with no
   target is skipped (`no_declared_targets`).
5. **Converge.** Each selected candidate runs the canonical converge path
   (`handlers.handle_converge`) with a request v2 body
   (`docs/contracts/converge/request.v2.md`): the tenant's cartridge pinned to
   the revision the targets were checked against, the candidate's node, the
   targets, the candidate's `entity` and `entity_id`, and the idempotency key
   `sweep-<sha256(sweep_id, entity_id)>`. The converge v2 response body is
   stored as the result, unchanged.

## 4. Budget rule

* Effective daily budget = min(`daily_budget_usd`, `L9_SWEEP_MAX_DAILY_BUDGET_USD`),
  per tenant per UTC day.
* The day's spend lives in a persisted ledger (`sweep_budget_ledger`, one row
  per tenant per UTC day; migration 004), shared by every sweep of the tenant
  that day.
* Before each converge, its worst-case cost is reserved:
  `max_budget_tokens × token_rate_usd_per_1k / 1000` — the token ceiling the
  converge loop enforces, at the configured rate. The reservation is one
  conditional update (`spent + reserved + estimate <= cap`), so concurrent
  sweeps cannot pass the cap together.
* When a reservation does not fit, the sweep stops: that candidate and every
  lower-ranked one are skipped (`budget_exhausted`).
* After the converge, the reservation is released and the actual cost
  (`tokens_used × token_rate_usd_per_1k / 1000`) is recorded. A response
  without a usable `tokens_used`, or a converge that raised, is charged the full
  reservation: unknown spend is not assumed to be zero.

## 5. Failure rule

Nothing is enriched without CEG's readiness for that candidate, and nothing
stands in for it: there is no local readiness estimate.

| Condition | Skip reason (per candidate) |
| --- | --- |
| Batch reply `{"status": "disabled"}` | `readiness_disabled` |
| Batch reply with any other status than `success`, `ok`, `completed`, `partial` (an absent status is read from `entities` alone) | `readiness_refused` |
| Gate or CEG failure (transport error, failure packet) | `readiness_failed` |
| Reply without an `entities` list, or an entry whose `entity_health` is malformed | `readiness_malformed` |
| Candidate absent from `entities[]` (see `total_truncated`, `cost_ceiling_reached`, `total_skipped`) | `readiness_absent` |
| Cartridge cannot be loaded | `cartridge_unavailable` |
| Budget ledger cannot be read or written | `budget_ledger_unavailable` — this and every remaining candidate; nothing further is enriched |
| Converge raised | `converge_failed` (charged the full reservation) |
| Unexpected runner error | `sweep_failed` for every candidate not yet handled |

## 6. `enrichment-sweep-results`

### 6.1 Request

`{"sweep_id": "<id>", "cursor": "<next_cursor from the previous page>"}` —
`cursor` is optional.

### 6.2 Response

```json
{
  "sweep_id": "<id>",
  "state": "running | completed",
  "results": [{"entity_id": "<id>", "response": {"...": "the converge v2 response body, as returned"}}],
  "next_cursor": "<string> | null",
  "budget": {"effective_usd": 2.5, "spent_usd": 2.0, "remaining_usd": 0.5},
  "skipped": [{"entity_id": "<id>", "reason": "<reason>"}],
  "stats": {"enriched": 2, "targets_dropped_undeclared": 0, "<reason>": 1}
}
```

* Results are in enrichment order. `next_cursor` is `null` once the sweep is
  completed and the last page has been returned; while it is running, poll with
  the returned cursor.
* `budget` is the tenant's ledger for the sweep's UTC day against this sweep's
  effective budget. `remaining_usd` also subtracts reservations in flight.
* `stats` is additive to the lane's response shape: counters by skip reason
  plus `enriched` and `targets_dropped_undeclared`.

### 6.3 Read-only and retention

`enrichment-sweep-results` is strictly read-only. It writes nothing (no ledger,
no sweep, no result, no fetch marker), never starts or resumes a sweep, and a
replayed request — same `sweep_id`, same `cursor` — returns the same page. A
page changes only because the background run of `enrichment-sweep` added
results or completed in between.

A sweep is retained for `L9_SWEEP_RESULT_TTL_HOURS` after it completes. Past
that, `enrichment-sweep-results` answers `sweep_not_found` without deleting
anything; the expired rows are deleted by the next `enrichment-sweep` call,
the only action that writes. A running sweep does not expire.

## 7. Refusals

Both actions raise (the SDK answers a `failure` packet) with a message that
begins with the code:

| Code | Condition |
| --- | --- |
| `disabled` | `L9_ENRICHMENT_SWEEP_ENABLED` is false. |
| `invalid_request` | Envelope invalid: unknown members, missing or empty `candidates`, a non-positive or non-numeric `daily_budget_usd`, a missing `idempotency_key`, a bad `cursor`. |
| `cartridge_unavailable` | The tenant's cartridge cannot be loaded. |
| `sweep_store_unavailable` | The database cannot record or read the sweep. |
| `sweep_not_found` | No such sweep for this tenant (or it has expired). |
