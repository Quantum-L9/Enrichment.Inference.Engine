# Converge response v2

| | |
|---|---|
| Status | **FROZEN 2026-11-13** by the CTO integration session `session_017xwbxxupo1nc2CvFZN6ewa`. A change is a new revision, not an edit. **Not implemented:** nothing in `app/` emits it yet. |
| Contract id | `eie.converge.response/v2`, paired with the request constant `eie.converge.request/v2`. |
| Extends | The v1 converge response: `app.models.schemas.EnrichResponse`, `docs/contracts/converge/v1.md`, canonical fixture `contracts/converge_response.json`. |
| Sibling contracts | `request.v2.md` (campaign A) and `v1.md` (campaign C). This file does not define them. |
| Transport | Payload of a `TransportPacket` answered through Gate. This file defines a payload, not a transport or a route. |
| Examples | `docs/contracts/converge/examples/response.v2.*.json`. Synthetic data only. |

## 1. What v2 adds

v1 answers "what value" per field (`fields`) and one aggregate `confidence`.
It cannot say **what kind of evidence** stands behind a value, so a consumer
cannot tell a value its own system of record supplied from one a provider
asserted, and a field with no evidence is indistinguishable from a field that
was never asked for.

v2 is **additive**. Every v1 key keeps its name, type and meaning. Three
top-level keys are added:

| Key | Type | Meaning |
|---|---|---|
| `contract_version` | string | `eie.converge.response/v2`. Absent means the payload is v1. |
| `field_evidence` | object, field name → FieldEvidence | One entry per requested field (section 2). |
| `proposals` | array of TypedProposal | Reviewable groupings of fields about one subject (section 5). |

A v1 consumer that ignores unknown keys reads a v2 payload exactly as it reads
v1 (section 7).

## 2. FieldEvidence

| Key | Type | Required | Meaning |
|---|---|---|---|
| `state` | string | always | Evidence state (section 3). |
| `admitted` | boolean | always | `true` exactly when the field's value is in the top-level `fields`. |
| `value` | any JSON value | for `observed`, `inferred`, `external_claim`; **forbidden** for `unknown` and `conflicting` | The value. Equals `fields[name]` when `admitted`. |
| `unit_status` | string | whenever `value` is present | `stated`, `not_applicable` or `unknown`. |
| `unit` | string | exactly when `unit_status` is `stated` | Unit of `value`, as the source stated it. No conversion is implied. |
| `confidence` | number in [0, 1] or `null` | always | Field confidence (section 4). `null` for `unknown` and `conflicting`, and for `observed` unless the consumer stated one. |
| `sources` | array of Source | always; non-empty for `observed` and `external_claim` | Where the value came from. |
| `derivation` | Derivation or `null` | non-null exactly when `state` is `inferred` | How the value was derived. |
| `identity` | Identity | always | Which subject the field describes. |
| `conflict` | Conflict or `null` | non-null exactly when `state` is `conflicting` | The disagreeing candidates. |
| `unknown_reason` | string or `null` | non-null exactly when `state` is `unknown` | Why there is no value. |

**Source**

| Key | Type | Meaning |
|---|---|---|
| `source_type` | string | `request_entity`, `request_observation`, `provider` or `kb`. |
| `source_ref` | string | Resolvable reference inside that source type: an entity field path, an observation id, a provider reply id, a KB fragment id. |
| `method` | string | How the source produced the value, for example `consumer_supplied` or `provider_reply`. |
| `independent` | boolean or `null` | Whether this source is independent of the others listed. `null` means unknown, which is the only value EIE can state today: no provider reply carries per-field provenance. |

**Derivation**

| Key | Type | Meaning |
|---|---|---|
| `method` | string | Rule or procedure name. |
| `rule_or_model_version` | string | Version of that rule or model. Required: an inference without a version is not traceable. |
| `inputs` | array of string | Names of the fields the value was derived from. Each must have its own `field_evidence` entry. |
| `kb_fragment_ids` | array of string | KB fragments consulted; a subset of the top-level `kb_fragment_ids`. |

**Identity**

| Key | Type | Meaning |
|---|---|---|
| `subject_ref` | string | The consumer's identifier for the subject, echoed from the request (`entity.id`). EIE never mints one. |
| `subject_type` | string or `null` | Echo of the request's `subject.type`. Opaque to this contract; `null` when the request carries no `subject`. Never derived. |
| `role` | string or `null` | Echo of the request's `subject.role`. Opaque to this contract; `null` when the request states none. Never derived. |

`subject_type` and `role` are carried, not interpreted. This contract names no
industry, role or entity type; the examples use values a domain spec would
declare.

**Conflict**

| Key | Type | Meaning |
|---|---|---|
| `candidates` | array, at least 2 | Each has `value`, `unit_status`, `unit` (when stated), `confidence` (number or `null`) and `sources`. Candidate values are pairwise different. |
| `resolution` | `null` | Always `null` in an EIE response. EIE reports a conflict; it does not resolve one. |

## 3. Evidence states

| State | Meaning | Value present | May be admitted |
|---|---|---|---|
| `observed` | The consumer supplied the value in the request (the entity, or an observation in the v2 request). EIE repeats it as evidence; EIE did not establish it. | yes | **never** |
| `inferred` | A versioned deterministic rule derived the value from other fields of this response. | yes | yes |
| `external_claim` | A source outside the consumer and outside EIE's rules asserted the value. Nothing in this response verifies it. | yes | yes |
| `conflicting` | Sources assert different values and no rule in this contract picks one. | no (see `conflict.candidates`) | **never** |
| `unknown` | No usable evidence. | no | **never** |

Rules that hold for every response:

1. **No upgrade.** An inference whose inputs include an `external_claim` is
   `inferred`, and its `derivation.inputs` names that input, so the weaker
   basis stays traceable. Nothing becomes `observed` by being repeated,
   agreed with or inferred from.
2. **Unknown and conflicting are never coerced.** They carry no `value`, no
   default, no placeholder and no best guess, and they never appear in `fields`.
3. **A state describes evidence, not acceptance.** A value below its field
   threshold keeps its real state with `admitted: false`. It is not relabelled
   `unknown`: the evidence exists, it was not strong enough.
4. **`observed` is scoped to the request, and is evidence only.** It says the
   consumer sent the value. It does not say the value is true, current or
   verified. It is never admitted: v1 `fields` holds only values the run
   produced (enriched, inferred or graph-seeded;
   `app/engines/convergence_controller.py::_assemble_convergence_response`),
   and v2 keeps that. Echoing a request value into `fields` would present the
   consumer's own input back to it as a new result. Admission governs this
   response only: since the operator ruling of 2026-11-24 the converge graph
   leg also syncs observed values, each labelled `observed` and never
   `enriched` (`docs/contracts/data/graph_sync_intents.md` *Field set and
   provenance*). Nothing in this response changes because of it.
5. **One state per entry.** When a source asserts a value for a field the
   request also supplied, the entry reports the produced value (`inferred` or
   `external_claim`) and may list the request among its `sources`; a
   disagreement between them is `conflicting`.

`unknown_reason` is one of `no_evidence`, `source_unavailable`,
`budget_exhausted`, `not_attempted`.

Relation to `contracts/feature_evidence` (`value_state`): the two vocabularies
overlap on `observed`, `inferred`, `unknown` and `conflicting`. Whether
`external_claim` here is the same thing as `reported` there is **Unknown**;
this contract declares no equivalence and no mapping.

## 4. Confidence

- `field_evidence[*].confidence` is the per-field score the consensus engine
  already computes: a **bounded, uncalibrated score** in [0, 1]. It is not a
  probability and not an accuracy estimate, and this contract does not let a
  consumer present it as either.
- A source that states no confidence contributes none. Missing, boolean,
  non-numeric and non-finite confidence is unusable; it is never defaulted to a
  positive number. `unknown` and `conflicting` carry `confidence: null`.
- EIE does not score a value it did not assess. An `observed` field carries the
  confidence the consumer stated with it, or `null`; EIE never assigns one, and
  in particular never assigns 1.0 for "the consumer said so".
- The top-level `confidence` and `uncertainty_score` keep their v1 meaning and
  the uncertainty policy that governs them (`D9-v2`,
  `app/engines/convergence/confidence_tracker.py`). v2 changes neither the
  formulas nor the population: readiness and both uncertainty scores are
  measured over the tracker's deciding fields (the requested fields when the
  request names a schema), a field is ready only when its value is present and
  its effective confidence meets its own threshold, a `null` confidence has
  effective confidence 0, and no aggregate overrides that.
- The examples follow those formulas: `uncertainty_score` is
  `10 * (1 - mean effective confidence)` over the requested fields, and
  `confidence` is the mean confidence of the fields in `fields`.

## 5. TypedProposal

A proposal groups fields about one subject for human review. It carries no
values of its own; it points at `field_evidence` entries.

| Key | Type | Meaning |
|---|---|---|
| `proposal_id` | string | Unique within the response. |
| `proposal_type` | string | Domain-declared kind of proposal. Opaque to this contract. |
| `identity` | Identity | The subject the proposal is about. Every referenced field has the same `identity`. |
| `fields` | array of string | Names of `field_evidence` entries, **including** `unknown` and `conflicting` ones, so a reviewer sees what is missing. |
| `admitted_fields` | array of string | The subset with `admitted: true`. |
| `review` | object | `required` is always `true`. `reasons` lists why, drawn from `contains_external_claim`, `contains_inferred`, `contains_conflict`, `contains_unknown`, `contains_unadmitted` (an `inferred` or `external_claim` field below its threshold). |

A proposal is a proposal. EIE writes nothing to the consumer's system; converge
performs no writeback in v1 and v2 does not add one.

## 6. Fail-closed semantics

**Producer.** A v2 response is valid only when all of these hold. A producer
that cannot satisfy them returns `state: "failed"` with a `failure_reason`
rather than a partial v2 body.

1. `field_evidence` has exactly one entry for every field the request asked
   for. A requested field with nothing to report is an explicit `unknown`
   entry, never a missing key. Entries for fields the request supplied but did
   not ask for are allowed; they are always `observed`.
2. The keys of `fields` are exactly the entries with `admitted: true`, and each
   `fields[name]` equals that entry's `value`.
3. Every entry satisfies the presence rules of section 2 for its state.
4. `state` is `completed`, `degraded` or `failed`, as v1 defines them.
   - `failed`: `fields` is empty, `proposals` is empty and no entry has
     `admitted: true`. Evidence gathered before the failure may still be
     listed, unadmitted.
   - `degraded` (the deadline stopped the run): as v1, `fields` is empty or a
     partial subset, and rule 2 still holds for whatever it contains.
     `proposals` is empty: a partial result is not a proposal.
5. `proposals` is always present, possibly empty.

**Refusals.** A request that does not resolve is refused as a whole: `state`
is `failed` and `failure_reason` begins with one of the codes below. A refusal
is never `degraded`: a request that did not resolve was never run. The first
thirteen codes and their conditions are the table in `request.v2.md` section 5;
this contract adopts them unchanged and adds the last.

| Code | Condition |
|---|---|
| `unsupported_contract`, `invalid_request`, `conflicting_members`, `cartridge_not_found`, `cartridge_refused`, `cartridge_pin_mismatch`, `tenant_cartridge_mismatch`, `node_not_declared`, `target_not_declared`, `target_type_mismatch`, `duplicate_target`, `subject_not_declared`, `subject_target_mismatch` | As defined in `request.v2.md` section 5. |
| `v2_member_without_contract` | The request carries a v2 member but not the `eie.converge.request/v2` contract constant. |

**Consumer.**

1. A payload without `contract_version` is v1. Do not read v2 meaning into it.
2. An unrecognized `contract_version`, an unrecognized `state`, a requested
   field with no entry, or an entry that breaks section 2: reject the evidence
   for that response. Do not repair it, default it or fall back to `fields`
   alone as if it were evidenced.
3. Never write an `unknown` or `conflicting` field. Never treat `admitted` as
   verification, and never treat `observed` as more than "the request said so".

## 7. Compatibility with the v1 consumer

The v1 Odoo mapper (`IB-Odoo_19`
`plasticos_gate/services/gate_mappers.py::map_converge_response`) reads the v1
keys by name and keeps the whole payload as `raw`. For a v2 payload it
therefore produces the same result as for the v1 payload with the same v1 keys:
`status` is `ok` only for `state: "completed"` with no `failure_reason`,
`final_fields` is `fields`, and `field_evidence` and `proposals` are reachable
only through `raw`. Because `fields` holds admitted values only (section 6),
that mapper can never see an `unknown` or `conflicting` field as a value.

This is checked on the EIE side against vendored expectations
(`tests/fixtures/odoo_parity/`, separate change). It is a parity check of
fixtures, not proof that a running Odoo persists v2 evidence: Odoo does not
read `field_evidence` today.

## 8. Examples

| File | Shows |
|---|---|
| `examples/response.v2.supplier-material.json` | Evidence-only `observed`, `external_claim`, `inferred`, an unadmitted `external_claim`, and an explicit `unknown`. |
| `examples/response.v2.supplier-facility.json` | Stated units, `not_applicable` units, an admitted contact field. |
| `examples/response.v2.buyer-material.json` | A `conflicting` field with two candidates and no resolution. |
| `examples/response.v2.buyer-facility.json` | An `inferred` field whose input is an `external_claim`. |
| `examples/response.v2.failed.json` | A failed run: nothing admitted, no proposals. |

## 9. Decisions at the freeze

1. **`external_claim` and FeatureEvidence `reported`.** Whether they are the
   same state stays explicitly **Unknown**. No equivalence, no mapping.
2. **Subject.** `identity.subject_type` and `identity.role` echo the request's
   `subject.type` and `subject.role`.
3. **`proposals`** is always present, possibly empty.
4. **Known consequence, not a defect.** A field the request both asks for and
   supplies, and that no source speaks to, is `observed` with `null`
   confidence. Under `D9-v2` it is a deciding field with effective confidence
   0, so it is never ready. v2.0 keeps `D9-v2` unchanged; this is intended.
   The examples avoid the case: their `observed` fields were supplied, not
   requested.
5. **`independent`** ships as an explicit `null` wherever independence is
   unknown. It is stated, not omitted.

## 10. Revision 1 (2026-11-13, additive, CTO)

Frozen by the CTO integration session `session_017xwbxxupo1nc2CvFZN6ewa`. The constant
stays `eie.converge.response/v2`. The revision lets a consumer answer from the response
alone: which record it is about, the state of each field, what supports each value, and
which cartridge revision and field definitions governed the result.

### 10.1 `resolution` (required on every v2 response whose request resolved)

What EIE actually loaded. This is not an echo of the request: the request's `version` and
`sha256` pins were checked against these values before any provider call.

| Key | Type | Meaning |
|---|---|---|
| `cartridge_id` | string | The loaded cartridge. |
| `cartridge_version` | string | Its loaded version. |
| `cartridge_sha256` | string | The digest of the published `spec.yaml` bytes actually served. It is always present, even when the request pinned only `version`. |
| `node` | string | The node the entity was resolved as. |

It is absent only on a refusal (section 6 codes), where nothing was resolved.

### 10.2 `field_evidence[*].declaration` (required)

The cartridge's declaration for that field, exactly as loaded: a declaration object in the
shared typed syntax (`app/engines/spec_types.py`), plus the declared `unit` when the
cartridge states one. A field without a declaration cannot have an entry, because
undeclared targets are refused.

### 10.3 Source additions

- `source_type` gains `graph`: a value seeded from the graph return channel.
- A `provider` source may carry `citations`: an array of URL strings the provider returned
  for the whole reply, with `citation_granularity: "reply"`. They support the reply as a
  whole. They are never presented as proof of one specific field, because providers do not
  attribute citations per field.
