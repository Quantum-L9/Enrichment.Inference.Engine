# Converge request v2

> **Status: FROZEN 2026-11-13 by the CTO (session_017xwbxxupo1nc2CvFZN6ewa).**
> Drafted by Campaign A (lane A-DOC). A change to this document is a contract
> change and needs a new CTO decision. Frozen is not implemented: the EIE side
> lands with lane A3e. Sibling documents: `v1.md` (Campaign C, the request the
> runtime accepts today) and `response.v2.md` (Campaign B, contract constant
> `eie.converge.response/v2`).

L9_META:
  tier: 2
  domain: convergence
  authority: EIE Two-Campaign Strategy Lock v0.1.0, Campaign A lane A-DOC

## 1. Scope

The body of a `converge` action carried as a TransportPacket payload and routed
by Gate. Gate is the only route to the node; this contract adds no endpoint, no
peer call and no transport field. The tenant is the one the chassis resolved
from the packet and hands to the handler. It is never read from the body.

v2 extends v1. It adds five members to the v1 body and changes nothing in it:

| Member | Purpose |
| --- | --- |
| `contract` | Selects v2. Its absence selects v1. |
| `cartridge` | Names and pins the cartridge that directs the run. |
| `node` | Names the cartridge node the entity is an instance of. |
| `subject` | Carries the subject type and commercial role of what the targets describe. |
| `targets` | Names the typed fields the caller wants enriched. |

## 2. What v2 replaces

Read from `app/engines/handlers.py` at `main` 7f4bac2. Today a converge request
reaches its cartridge and fields implicitly:

| Concern | v1 behavior today | v2 |
| --- | --- | --- |
| Cartridge | First non-empty of `domain_id`, `domain`, `kb_context`, `object_type`. | `cartridge.id` only. No fallback chain. |
| Unknown cartridge | Logged; the run continues with no hints and no rules. | Refused. |
| Revision | Whatever revision the reader currently serves. | Pinned by `version` (required) and optionally `sha256`; a mismatch is refused. |
| Tenant | Not compared with the cartridge. | `cartridge.id` must equal the chassis-resolved tenant. |
| Node | `node_label`, else the spec's only node, else no hints (logged). | `node` is required. |
| Fields | `schema`, a free `{name: type-string}` map not checked against the cartridge. | `targets`, each checked against the cartridge's declaration. |

## 3. Members

### 3.1 `contract` (required for v2)

The constant string `eie.converge.request/v2`.

- Absent: the body is v1 and is handled exactly as today, unless it carries a
  v2 member (`cartridge`, `node`, `subject` or `targets`). That body is refused
  (`v2_member_without_contract`): it is a v2 request that forgot `contract`,
  and running it as v1 would silently drop what the caller asked for.
- Present with any other value: refused (`unsupported_contract`).

### 3.2 `cartridge` (required)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | Cartridge id. Must satisfy `domain_yaml_reader.is_legal_tenant_id`, the complete predicate the reader applies (lowercase, the tenant-id grammar, and not the reserved `system` prefix). An id that fails it is `invalid_request`. |
| `version` | string | yes | Semantic version the caller expects. |
| `sha256` | string | no | Digest of the published `spec.yaml` bytes the caller expects. |

`id` must equal the tenant the chassis resolved from the packet. This is the
identity contract (operator decision 2026-10-22: tenant id == spec id), which
v2 enforces and v1 does not. A different id is refused
(`tenant_cartridge_mismatch`). v2.0 has no grant table; letting a tenant use
another cartridge needs a new operator decision.

`version` must equal the loaded revision's (`DomainEnrichmentConfig.version`).
`sha256`, when present, must equal `.source_sha256`. A `sha256` pin against an
unpublished (legacy, digestless) cartridge cannot be checked and is refused
rather than ignored.

### 3.3 `node` (required)

A node label the cartridge declares. Required even when the cartridge has one
node, so adding a node to a cartridge can never silently change what a request
means.

### 3.4 `targets` (required, non-empty)

A list of field targets on the named node.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `field` | string | yes | A property the node declares. |
| `type` | declaration | no | The caller's expected type as a declaration object in the shared typed syntax (`app/engines/spec_types.py`, `validate_declaration`): `{type: float}`, `{type: list, items: {...}}` or `{type: map, properties: [...]}`. Always an object, scalars included; a bare string such as `"float"` is `invalid_request`, so no normalization step exists. |
| `min_confidence` | number in [0, 1] | no | The caller's bar for this field. Same meaning as a v1 `field_thresholds` entry. |

Rules:

- `field` names must be unique within `targets`.
- When `type` is present it must equal the cartridge's declaration for that
  field, shape for shape. EIE does not coerce between them.
- When `type` is absent the cartridge's declaration is the type.
- A v2 body must not also carry `schema` or `field_thresholds`; `targets`
  replaces both, and two sources for the same fact are refused.

**Material and facility targets.** Shared code has no notion of "material" or
"facility". They are node labels and subject types the PlasticOS cartridge declares. A
request addresses them by `node`, `subject` and explicit `targets`, exactly as a request against any other cartridge addresses its own
nodes. The freight-carriers test cartridge (lane A5) addresses `Carrier` the
same way.

**Eligibility is not write authority.** Any declared property of the named node
may be a target, including evidence-only ones. Being a target makes a field
eligible for enrichment and for a typed proposal in the response. It authorizes
no write to any system. Fields the cartridge does not declare, which includes
every CEG-owned matching and ranking field, cannot be targets.

### 3.5 `subject` (optional)

`subject` is the key that carries the subject type and the commercial role of
the thing the targets describe. `response.v2.md` refers to this key by the
paths `subject.type` and `subject.role`.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `type` | string | yes, when `subject` is present | Subject type, e.g. `material` or `facility` in PlasticOS. |
| `role` | string | no | Commercial role, e.g. `supplier` or `buyer` in PlasticOS. |

Both are **opaque, cartridge-declared strings**. Shared code compares them for
equality against what the loaded cartridge declares and gives them no other
meaning: it never branches on a particular value, and the examples above are
PlasticOS vocabulary, not contract vocabulary. One request converges one
entity, so `subject` applies to every entry of `targets`.

- A value the cartridge does not declare is refused (`subject_not_declared`).
- The cartridge declares its subject vocabulary in its own spec: subject types,
  each bound to a node, and a list of roles. Lane A2s owns the shape of that
  declaration.
- `node` must be the node the subject type is bound to
  (`subject_target_mismatch`).
- A subject type never stands in for a field list. v2.0 has no
  subject-to-fields expansion; `targets` stay explicit.
- A cartridge that declares no subject vocabulary accepts no `subject`; a
  request that sends one is refused, never silently stripped.

### 3.6 Members carried over from v1 unchanged

The authority for the v1 body is `v1.md`. Its "eight-field" request is the
eight allowlisted partner fields the producer sends in `schema` (`name`,
`website`, `city`, `zip`, `street`, `street2`, `email`, `phone`), not eight
top-level members. v2 carries over `entity`, `entity_id`, `object_type`,
`objective`, `consensus_threshold`, `max_variations`, `max_passes`,
`idempotency_key`, and producer context blocks such as `odoo`. `schema` is not
carried over: `targets` replaces it (section 3.4). In v2, `object_type` is a source-system label only and never
selects a cartridge. `domain`, `domain_id`, `node_label` and `kb_context` do not
select a cartridge or node in v2; if present they must agree with `cartridge.id`
/ `node` or the request is refused.

## 4. Example

```json
{
  "contract": "eie.converge.request/v2",
  "cartridge": {"id": "plasticos", "version": "0.4.0"},
  "node": "Facility",
  "subject": {"type": "facility", "role": "supplier"},
  "targets": [
    {"field": "contamination_tolerance", "type": {"type": "float"}, "min_confidence": 0.8},
    {"field": "processed_polymers"},
    {"field": "capacity_tons_month", "type": {"type": "float"}}
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
  belongs to `response.v2.md`, which adopts this table.

| Code | Condition |
| --- | --- |
| `unsupported_contract` | `contract` is present and is not the v2 constant. |
| `v2_member_without_contract` | `cartridge`, `node`, `subject` or `targets` on a body without `contract`. |
| `invalid_request` | A required member (including `cartridge.version`) is missing, mistyped, or `targets` is empty. |
| `conflicting_members` | `schema`/`field_thresholds` present, or a v1 selector disagrees with `cartridge.id`/`node`. |
| `cartridge_not_found` | No cartridge with that id can be loaded. |
| `cartridge_refused` | The reader refused the revision (`DomainPublicationError`). |
| `cartridge_pin_mismatch` | `version` or `sha256` differs from the loaded revision, or cannot be checked. |
| `tenant_cartridge_mismatch` | `cartridge.id` is not the chassis-resolved tenant. |
| `node_not_declared` | The cartridge declares no such node. |
| `target_not_declared` | A target field is not a property of the node. |
| `target_type_mismatch` | A target `type` differs from the declaration. |
| `duplicate_target` | A field appears twice in `targets`. |
| `subject_not_declared` | `subject.type` or `subject.role` is not declared by the cartridge. |
| `subject_target_mismatch` | `node` is not the node the cartridge binds the subject type to. |

**Response states.** v2 builds on v1's three states (`v1.md`): `completed`,
`degraded` (the deadline stopped the request) and `failed`. Every refusal in
the table above is `failed`: a request that did not resolve was never run, so
it is not `degraded`. `degraded` is reserved for a resolved request that ran
and ran out of time.

An unknown, unreadable or unresolved input is never treated as absent, defaulted
or satisfied (L9-UNKNOWN-001). An unknown top-level member is refused as
`invalid_request` rather than ignored, so a misspelled v2 member cannot silently
fall back to a default.

## 6. Compatibility with v1

- A body without `contract` and without a v2 member is v1. Every such body
  valid today stays valid and behaves identically; the live producer payload
  pinned by `tests/contracts/test_canonical_converge_runtime_contract.py`
  carries no v2 member and is the witness.
- A body without `contract` that carries a v2 member is refused
  (`v2_member_without_contract`).
- v1 does not compare tenant and cartridge; that existing behavior, including
  the contract test that converges tenant `tenant-a` on `plasticos`, is
  unchanged.
- v1's fail-soft behaviors (unknown cartridge, ambiguous node) are unchanged for
  v1 callers. v2 does not tighten v1.

## 7. Not in this contract

Odoo writeback, the response shape and its proposal types (`response.v2.md`),
evidence acquisition and provenance (Campaign B), ingress and handler contract
versions (Campaign C), batch converge, scoring, matching and ranking (CEG).

## 8. Decisions recorded at freeze

Decided by the CTO on 2026-11-13; each is reflected in the sections above.

1. **Tenant to cartridge.** `cartridge.id` must equal the chassis-resolved
   tenant. No grant table in v2.0.
2. **Pin strength.** `cartridge.version` is required. `sha256` is optional and
   must match when present.
3. **v2 members without `contract`.** Refused with `v2_member_without_contract`.
4. **Subject.** Request-level. The cartridge declares the vocabulary (lane A2s
   owns the shape). No subject-to-fields expansion in v2.0.
5. **"Eight-field" v1.** The eight partner fields in v1 `schema`; `v1.md` is
   the authority for the v1 body.

## 9. Revision 1 (2026-11-13, additive, CTO)

Frozen by the CTO integration session `session_017xwbxxupo1nc2CvFZN6ewa`. The contract
constant is still `eie.converge.request/v2`; the revision adds one optional member and
changes nothing above.

### 9.1 `observations` (optional)

Facts the consumer already holds about the entity, such as transaction history. They are
offered as evidence. They are not targets and not instructions.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `field` | string | yes | A property declared on `node`. Need not be a target. |
| `value` | any JSON value | yes | The observed value, as the consumer holds it. |
| `unit` | string | no | Unit of `value`, as the consumer states it. No conversion is implied. |
| `source_kind` | string | yes | Opaque consumer label for where it came from, for example `transaction`. |
| `source_ref` | string | yes | The consumer's record reference, for example `sale.order:123`. |
| `observed_at` | string (RFC 3339) | no | When the consumer observed it. |
| `confidence` | number in [0, 1] | no | The consumer's own confidence. EIE never assigns one. |

Rules:
- A `field` the node does not declare is refused as `target_not_declared`.
- An observation is evidence only. In the response it is reported with state `observed`
  and source type `request_observation`, and it is never admitted into `fields`.
- An observation speaks only to its own field. It is never generalized into another
  field, another subject or a capability.
- `observations` on a body without `contract` is `v2_member_without_contract`.
