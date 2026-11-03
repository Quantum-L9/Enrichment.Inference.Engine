# Schema proposal node ownership — contract v1

L9_META:
  tier: 3
  authority: native-domain-completion-v3-g27r

## Authority and identity

An executable native evidence proposal is identified by `(tenant_id, domain, batch_run_id, node_label, field_name)`. The tenant equals the domain and the native spec identity. Ownership is the ontology node recorded at discovery, not the enrichment provider, CRM object type, approval destination, YAML position, or operator label. A label uses the native identifier grammar `[A-Za-z_][A-Za-z0-9_]*` and is at most 128 characters.

`FieldProposal.node_label` and `ApprovalDecision.node_label` default to `None` only for lossless decoding of legacy diagnostic/history data. New executable fields also carry `discovery_spec_sha256` and `ownership_contract_version=1`. The containing `SchemaProposalSet` carries `tenant_id`, `domain`, the same digest, and version `1`. Every field must agree with the containing set. The discovery digest is exactly 64 lowercase hexadecimal characters and identifies original complete discovery YAML bytes, not a reserialization chosen at approval.

Historical absent/NULL binding remains unbound. Native execution refuses it; an approval label cannot manufacture provenance. Recovery requires new owner-bound discovery and review, preserving the old history.

## Discovery interface

```python
propose(batch_results, current_yaml, domain="", *,
        node_label=None, discovery_spec_sha256=None, tenant_id=None)
```

The trusted discovery caller obtains and validates an exact native source snapshot before calling. `node_label` explicitly binds a single-owner batch; any result label must agree. Alternatively, each result explicitly carries `node_label`, allowing multiple owners. All owners are checked against the discovery ontology before accumulation. Existing-property filtering and statistical denominators are owner-specific; two nodes can independently propose the same field name with different types, values, and sources. A failed owner or qualifying source conflict refuses the whole call.

Calls without any ownership inputs remain draft-only compatibility diagnostics. Their returned fields/set have no binding and cannot execute against a native spec. This preserves historical diagnostic tests without treating them as native review evidence.

When a result contains `field_source_observations`, it is a complete `dict[str, list[str]]` of observed pass-level sources. Lists contain nonblank, trimmed strings. All observations are retained in the owner's source set; no default source or winner is selected. Missing/empty observations contribute zero confidence and cannot fabricate supported proposals. Without this explicit key, existing F005 confidence-record behavior remains available for offline diagnostic inputs. A qualifying owner/field with competing sources raises `SourceAttributionError`; evidence is presented deterministically as `Node.field` for bound discovery. EIE still proposes no CEG gates or scoring dimensions (ADR-010).

## Decision/application interfaces

```python
validate_ownership(proposal_set, approval_decisions)  # pair -> approved
applied_field_keys(approval_decisions, proposal_set) # [(node, field), ...]
apply(current_yaml, approval_decisions, proposal_set, *,
      node_label=None, allow_overwrite=False,
      source_sha256=None, source_digest=None)
```

Every native decision, including rejection, explicitly names a proposed owner/field pair. Duplicate same-owner fields or decisions and unknown pairs refuse. Same-name fields on different owners are distinct. `applied_field_keys` preserves proposal order. `applied_field_names` remains a validated single-owner compatibility API and refuses multi-owner proposal sets rather than collapsing identities.

`source_sha256` is an exact current-source assertion supplied by the snapshot reader. `source_digest` is an equivalent spelling for offline callers; conflicting aliases refuse. Native direct application requires an assertion equal to the retained discovery digest. The caller must compare the source snapshot before constructing an executable proposal from saved run state. Neither application nor replay reconstruction computes or stamps a historical digest from current dictionary data.

Application validates every recorded owner and optional label assertion before a no-op return. A supplied `node_label` must equal every proposal owner and cannot drop a mixed-owner subset. Approved fields are applied to their own recorded nodes using one copied candidate and one version bump. Any failure leaves the input unchanged. Existing field definitions cannot be overwritten without explicit permission; permitted type/source changes retain descriptions and other field metadata. Unrelated ontology nodes, inference rules, semantic configuration, and enrichment hints survive. Rejections and undecided fields never materialize.

`revision_from_approved` accepts explicit `tenant_id`, `discovery_spec_sha256`, and `ownership_contract_version` keyword binding. It preserves field originals and never infers/fills missing per-field ownership. Native application refuses reconstructed unbound legacy rows.

## Direct offline workflow

`materialize_approved_spec(..., tenant_id=..., node_label=None, source_sha256=None, source_digest=None)` validates the complete base, tenant/domain, pair identities, all owner labels, and source assertion before returning complete candidate bytes. `approve_and_publish` accepts the same ownership/source keywords and preflights candidate binding plus named review/digest inputs before audit. Its audit call retains the existing `persist_schema_proposal_batch(tenant_id, domain, batch_run_id, reviewed_by, decisions)` signature; every decided row includes `node_label`, `discovery_spec_sha256`, and `ownership_contract_version=1`.

Audit remains transactional evidence, not runtime activation. All-rejected bound batches may be audited but have no publication or revision bump. Audit failures do not activate candidates. Existing publication rules remain separate: immutable exact reviewed bytes, original named-review receipts, version/history rules, and CAS publication/rollback are unchanged. Publication failure after audit does not change the effective pointer and never automatically retries.

## Audited materialization and intentional rebase

```python
await materialize_audited_spec(base_spec, *, tenant_id, domain,
    node_label=None, batch_run_ids=(), allow_overwrite=False,
    discovery_specs=None, applied_field_keys=None)
# returns (complete_candidate_bytes, applied_batch_ids)
```

`discovery_specs` maps retained lowercase digests to their **exact original bytes**. Before constructing any candidate, every selected approved row is checked for tenant/domain, original reviewer and timezone-aware nonfuture review time, version-1 owner/digest binding, pair uniqueness, and consistent batch discovery digest. The source map must supply matching bytes; these are read through the native safe parser and complete native validator. Each recorded node must exist in both the original discovery artifact and the new complete base. Missing, forged, renamed, or unbound ownership refuses even when the destination field would otherwise be a same-type no-op.

This verified bridge deliberately permits a newer base for a candidate-only operation; it does not replace the historical digest with the new base digest. Existing-name/same-type skips and explicit type-conflict overwrite are scoped to the recorded owner. Each mixed-owner batch applies atomically with one version bump. The complete rebased bytes still need separate exact-byte review and publication. Selected/default unbound approvals refuse rather than being silently ignored. Rejected records remain history and never materialize.

The optional `applied_field_keys` output list is populated only after the whole operation succeeds. The return shape remains `(bytes, batches)` for compatibility; batch provenance is retained. No audit rows are rewritten, no approvals are restamped, and no effective pointer is changed by this bridge.

## Operator interface

`materialize-approved` requires complete `--base-spec`, `--tenant`, `--domain`, repeatable `--discovery-spec PATH`, and a new `--output` file. Each discovery file is read through the existing no-follow bounded native reader and keyed by its exact digest. A newer base is not substituted for a missing original artifact. Optional `--node-label` asserts all selected recorded owners; it cannot redirect a field or silently narrow a selected mixed-owner batch. Repeatable `--batch-run-id` selects whole approved batches. Output diagnostics include both `applied_approval_batches` and owner-qualified `applied_field_keys`.

The operator creates candidate files only. Publication and rollback remain separately requested, exact-review/CAS operations. This contract authorizes no deployment, live database operation, migration execution, or approval/promotion.

## Local acceptance evidence

Focused unit/integration tests cover two-node same-name suppression/statistics, per-owner F005 refusal, source observations, pair approval/rejection, atomic one-bump application, owner assertions, legacy refusal including no-ops, stale digest refusal, retained source verification before rebase, owner-scoped existing-field conflicts, reviewer/tenant failures, audit-before-publication, all-rejected no-publication, and candidate-only operator diagnostics. Storage/ORM/migration and API/state integration remain separately owned; parent-run unchanged mandatory gates bind the final integrated source.
