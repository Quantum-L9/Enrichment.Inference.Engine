# Converge request v2 — DRAFT

> **Status: DRAFT. Not frozen, not implemented, not binding.**
> Drafted by Campaign A (lane A-DOC) for the CTO to freeze. Until the CTO
> freezes it, nothing may be built against this document, and no field named
> here exists in `app/`. Sibling documents: `v1.md` (Campaign C, the request the
> runtime accepts today) and `response.v2.md` (Campaign B, the response).

L9_META:
  tier: 2
  domain: convergence
  authority: EIE Two-Campaign Strategy Lock v0.1.0, Campaign A lane A-DOC

## 1. Scope

The body of a `converge` action carried as a TransportPacket payload and routed
by Gate. Gate is the only route to the node; this contract adds no endpoint, no
peer call and no transport field. The tenant is the one the chassis resolved
from the packet and hands to the handler. It is never read from the body.

v2 extends v1. It adds four members to the v1 body and changes nothing in it:

| Member | Purpose |
| --- | --- |
| `contract` | Selects v2. Its absence selects v1. |
| `cartridge` | Names, and optionally pins, the cartridge that directs the run. |
| `node` | Names the cartridge node the entity is an instance of. |
| `targets` | Names the typed fields the caller wants enriched. |

## 2. What v2 replaces

Read from `app/engines/handlers.py` at `main` 7f4bac2. Today a converge request
reaches its cartridge and fields implicitly:

| Concern | v1 behavior today | v2 |
| --- | --- | --- |
| Cartridge | First non-empty of `domain_id`, `domain`, `kb_context`, `object_type`. | `cartridge.id` only. No fallback chain. |
| Unknown cartridge | Logged; the run continues with no hints and no rules. | Refused. |
| Revision | Whatever revision the reader currently serves. | Optionally pinned by `version` and `sha256`; a mismatch is refused. |
| Node | `node_label`, else the spec's only node, else no hints (logged). | `node` is required. |
| Fields | `schema`, a free `{name: type-string}` map not checked against the cartridge. | `targets`, each checked against the cartridge's declaration. |

## 3. Members

### 3.1 `contract` (required for v2)

The constant string `eie.converge.request/v2`.

- Absent: the body is v1 and is handled exactly as today. No v2 member is read.
- Present with any other value: refused (`unsupported_contract`). A v2 member
  on a body without `contract` is not an upgrade request; see section 6.

### 3.2 `cartridge` (required)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | Cartridge id, in the tenant-id grammar of `domain_yaml_reader.TENANT_ID_PATTERN`. |
| `version` | string | no | Semantic version the caller expects. |
| `sha256` | string | no | Digest of the published `spec.yaml` bytes the caller expects. |

A pin that is present must equal the loaded revision (`DomainEnrichmentConfig.version`,
`.source_sha256`). A `sha256` pin against an unpublished (legacy, digestless)
cartridge cannot be checked and is refused rather than ignored.

### 3.3 `node` (required)

A node label the cartridge declares. Required even when the cartridge has one
node, so adding a node to a cartridge can never silently change what a request
means.

### 3.4 `targets` (required, non-empty)

A list of field targets on the named node.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `field` | string | yes | A property the node declares. |
| `type` | declaration | no | The caller's expected type, in the shared typed syntax (`app/engines/spec_types.py`): a scalar name, or `{type: list, items: ...}` / `{type: map, properties: [...]}`. |
| `min_confidence` | number in [0, 1] | no | The caller's bar for this field. Same meaning as a v1 `field_thresholds` entry. |

Rules:

- `field` names must be unique within `targets`.
- When `type` is present it must equal the cartridge's declaration for that
  field, shape for shape. EIE does not coerce between them.
- When `type` is absent the cartridge's declaration is the type.
- A v2 body must not also carry `schema` or `field_thresholds`; `targets`
  replaces both, and two sources for the same fact are refused.

**Material and facility targets.** Shared code has no notion of "material" or
"facility". They are node labels (or, after lane A2s, cartridge-declared profile
concerns) of the PlasticOS cartridge. A request addresses them by `node` plus
`targets`, exactly as a request against any other cartridge addresses its own
nodes. The freight-carriers test cartridge (lane A5) addresses `Carrier` the
same way.

**Eligibility is not write authority.** Any declared property of the named node
may be a target, including evidence-only ones. Being a target makes a field
eligible for enrichment and for a typed proposal in the response. It authorizes
no write to any system. Fields the cartridge does not declare, which includes
every CEG-owned matching and ranking field, cannot be targets.

### 3.5 Members carried over from v1 unchanged

`entity`, `entity_id`, `object_type`, `objective`, `consensus_threshold`,
`max_variations`, `max_passes`, `idempotency_key`, and producer context blocks
such as `odoo`. In v2, `object_type` is a source-system label only and never
selects a cartridge. `domain`, `domain_id`, `node_label` and `kb_context` do not
select a cartridge or node in v2; if present they must agree with `cartridge.id`
/ `node` or the request is refused.

## 4. Example

```json
{
  "contract": "eie.converge.request/v2",
  "cartridge": {"id": "plasticos", "version": "0.4.0"},
  "node": "Facility",
  "targets": [
    {"field": "contamination_tolerance", "type": "float", "min_confidence": 0.8},
    {"field": "processed_polymers"},
    {"field": "capacity_tons_month", "type": "float"}
  ],
  "entity": {"id": "res.partner:55", "name": "Acme Recycling", "city": "Charlotte"},
  "object_type": "res.partner",
  "objective": "Enrich the facility profile",
  "max_variations": 5,
  "consensus_threshold": 0.8
}
```

## 5. Fail-closed semantics

A v2 request is resolved completely before any provider call. If any check
below fails, the request is refused as a whole:

- no provider call, no inference, no persistence, no graph or event side effect;
- no partial run on the targets that did resolve;
- no fallback to v1 resolution, to a default cartridge, to a default node, or
  to "converge without hints";
- the answer is an `EnrichResponse` with `state: "failed"` and a
  `failure_reason` beginning with the code below. The response contract for v2
  belongs to `response.v2.md`; these codes are proposed here for it to adopt.

| Code | Condition |
| --- | --- |
| `unsupported_contract` | `contract` is present and is not the v2 constant. |
| `invalid_request` | A required member is missing, mistyped, or `targets` is empty. |
| `conflicting_members` | `schema`/`field_thresholds` present, or a v1 selector disagrees with `cartridge.id`/`node`. |
| `cartridge_not_found` | No cartridge with that id can be loaded. |
| `cartridge_refused` | The reader refused the revision (`DomainPublicationError`). |
| `cartridge_pin_mismatch` | `version` or `sha256` differs from the loaded revision, or cannot be checked. |
| `tenant_cartridge_mismatch` | The tenant may not use this cartridge. See open question 1. |
| `node_not_declared` | The cartridge declares no such node. |
| `target_not_declared` | A target field is not a property of the node. |
| `target_type_mismatch` | A target `type` differs from the declaration. |
| `duplicate_target` | A field appears twice in `targets`. |

An unknown, unreadable or unresolved input is never treated as absent, defaulted
or satisfied (L9-UNKNOWN-001). An unknown top-level member is refused as
`invalid_request` rather than ignored, so a misspelled v2 member cannot silently
fall back to a default.

## 6. Compatibility with v1

- A body without `contract` is v1. Every v1 body valid today stays valid and
  behaves identically; the live producer payload pinned by
  `tests/contracts/test_canonical_converge_runtime_contract.py` is the witness.
- v2 members on a v1 body are not read. Whether they are ignored or refused is
  open question 3.
- v1's fail-soft behaviors (unknown cartridge, ambiguous node) are unchanged for
  v1 callers. v2 does not tighten v1.

## 7. Not in this contract

Odoo writeback, the response shape and its proposal types (`response.v2.md`),
evidence acquisition and provenance (Campaign B), ingress and handler contract
versions (Campaign C), batch converge, scoring, matching and ranking (CEG).

## 8. Open questions for the CTO

1. **Tenant to cartridge.** The identity contract (operator decision 2026-10-22,
   `domain_yaml_reader.py`) says tenant id == spec id. The converge handler does
   not enforce it today: the existing contract test converges tenant `tenant-a`
   on cartridge `plasticos`. Does v2 require `cartridge.id == tenant`, or an
   explicit tenant-to-cartridge grant? `tenant_cartridge_mismatch` is reserved
   for whichever is chosen.
2. **Pin strength.** Is `version` alone an acceptable pin, or must a v2 request
   against a published cartridge carry `sha256`?
3. **v2 members on a v1 body.** Ignore (strict v1 compatibility) or refuse
   (catches a caller that forgot `contract`)? This draft assumes ignore.
4. **Profile concerns.** If lane A2s declares material-profile and
   facility-profile concerns inside one node rather than as nodes, should a
   target group be addressable by concern name, expanding to its fields?
5. **"8-field v1 request."** The campaign brief describes v1 as an 8-field
   request. `EnrichRequest` at `main` declares 11 fields and the live producer
   payload sends 6 top-level members. Which 8 is meant is Unknown to this lane;
   `v1.md` is the authority and section 3.5 should be reconciled with it.
