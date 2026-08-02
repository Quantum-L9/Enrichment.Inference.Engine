# ADR-011: Single EIE side-effect coordinator

## Status
Accepted (TASK-021)

## Context
Post-enrichment work was duplicated across `handlers._persist_and_sync`,
`orchestration_layer.enrich_and_sync` (GraphSyncClient + PacketRouter), and
legacy `graph_sync_hooks`. That risked multiple persist/dispatch/event calls
per request.

## Decision
- `SideEffectCoordinator` is the sole authority for persist, Gate graph-sync,
  score-invalidate, and enrichment-completed emission after enrich.
- Idempotency is keyed by packet id, else payload idempotency key, else a
  tenant/entity hash.
- `enrich-and-sync` calls `handle_enrich` once and does not re-issue side effects.

## Consequences
One persistence/dispatch/event per semantic request. GraphSyncClient remains
for outcome feedback only, not enrich follow-up duplication.
