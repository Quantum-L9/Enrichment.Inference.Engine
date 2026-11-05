# Native EIE domain publication — local operator runbook

**Scope:** source-only implementation and isolated local rehearsal. This runbook does not authorize production promotion, deployment, mounts, push, PR, merge, issue mutation, or changes to CEG graph semantics. Real approver identities and actual platform mount behavior remain operator-supplied; test fixture reviewer strings are not production approvals.

## Authority and responsibility

The complete, immutable **published EIE YAML** is the sole effective domain authority. PostgreSQL `schema_proposals` is a durable decision audit, not a runtime spec store. Native consumers use the same published reader view and do not replay database approvals onto published bytes. A database outage cannot cause fallback to a seed, baked pack, or independently replayed registry revision.

EIE owns enrichment fields, enrichment hints, inference rules/provenance and resolution hints. ADR-010 prohibits EIE from generating or applying CEG match gates or ranking/scoring proposals. The existing `schema_proposer` still discovers evidence fields and applies explicitly approved field additions. Gate/scoring proposal classes remain for compatibility with historical audit data, but generated/reconstructed native proposal sets contain no ranking changes and application ignores such payloads.

CEG retains its own native schema, version and SHA-256. EIE and CEG YAML must **not** be made byte-identical or forced to share versions. Publication here touches the EIE store only; an independently validated compatible-pair record must bind the two separate artifact digests and the shared sync vocabulary before any separately authorized coordinated release. No compiler, compiler receipt, new Gate action or new production HTTP route is required.

## Layout and admission

```text
<operator-eie-root>/<tenant>/
  .publication.lock
  current.json
  revisions/
    <sha256>/
      spec.yaml
      receipt.json
```

`current.json` is `{ "schema": "eie.domain-current/v1", "sha256": "<64 lowercase hex>" }`. A receipt declares `schema: eie.domain-approval/v1`, `tenant_id`, `version`, `sha256`, `reviewed_input_sha256`, `reviewed_by`, `approval_ref`, timezone-aware `reviewed_at`, `previous_sha256` (null initially), and `approval_batches`.

The reviewed input hash must equal the SHA-256 of the **exact final spec bytes**. Neither a digest of the seed/base, nor a hash computed before applying approved field additions, is valid review evidence. Changing whitespace, fields, comments or serialization after review requires reviewing the changed bytes and supplying their new digest. The reviewer and approval reference are explicit caller inputs; the program never invents either, and an approval reference is evidence linkage, not authentication or a cryptographic signature.

Native validation is delegated to `app.engines.domain_yaml_reader.validate_native_spec`. Publication additionally refuses seed/draft markers, external inference-rule references, unbound or malformed review evidence, symlinks at every root/ancestor/tenant/revision/file boundary, path traversal, hardlinked sidecar/spec files, malformed/duplicate-key JSON, and artifacts exceeding native reader size limits. Published inference rules must be inline in the reviewed YAML, because revision content is exactly `spec.yaml` and `receipt.json`.

All publication operations use no-follow, directory-relative descriptors. Cooperating publishers serialize with a tenant lock, validate prior state, write and fsync both revision files in a staging directory, atomically install the whole revision, fsync its parent, then atomically replace and fsync `current.json`. Revision files are read-only and revisions are retained. Read-only permissions and hashes are defense-in-depth, **not** protection against an operator/root account that can rewrite the store; supply access controls and read-only runtime delivery separately.

## 1. Scan CRM metadata to a partial seed

Run from the repository using its existing Python 3.12 environment:

```bash
.venv/bin/python -m scripts.domain_spec_operator --help
.venv/bin/python -m scripts.domain_spec_operator scan-to-seed \
  --crm-metadata /absolute/operator/crm-fields.json \
  --template /absolute/operator/complete-eie-template.yaml \
  --tenant "$L9_TENANT_ID" \
  --source "$L9_SOURCE_IDENTIFIER" \
  --seed-output /absolute/operator/review/seed.yaml \
  --report-output /absolute/operator/review/scan.json
```

Metadata is a JSON field list or `{ "fields": [...] }`. Each entry requires technical `name`; optional `field_type` (or `type`) and `source_resource` are accepted. CRM record values and descriptions are deliberately not copied into the redacted evidence. Use a technical source identifier, never a token, credential-bearing URL, private record, or connection string.

The workflow reuses `scan_crm_fields` and `generate_seed_yaml`; it does not replace their engines. The native template can have a different tenant/vertical identifier; that identifier and its digest/version are provenance only. The seed's identity is the explicit tenant. Report JSON records tenant, source, template SHA-256, template version, timestamp, matched, unmapped, missing and ambiguous names. Normalization collisions are withheld and reported as ambiguous rather than accepted from a last-wins lookup. Incompatible source/domain field types are visible in `type_compatible` and need operator resolution.

The output seed is intentionally partial (`0.1.0-seed`, `draft_only: true`). It is **not** a complete native spec and cannot be activated. Output files must be new, distinct, nonsymlink files; existing output files are not overwritten. Review technical field names carefully, since names/provenance are not record-value redaction.

## 2. Complete native authoring and review field decisions

A domain owner supplies a complete EIE-native base with explicit top-level `name` and semantic `version`, list-shaped node/property declarations, required field meaning, reviewed enrichment hints, inline inference rules with provenance, resolution policy, and required shared sync references. Do not merely rename a seed's version or guess the first ontology node. Multi-node field application requires `node_label`; existing property definitions cannot change unless `allow_overwrite=True` is explicitly supplied. Allowed updates preserve unrelated property metadata, hints, rules and resolution sections.

The existing proposer produces discovery evidence. Approve/reject decisions must name proposed canonical fields explicitly. Undecided fields are not applied. Duplicate/conflicting decisions, duplicate field proposals and approvals of nonexistent/noncanonical fields are refused. HTTP approvals remain audit-only with `awaiting_operator_publication`; an HTTP acknowledgement is not activation.

Two supported offline paths follow.

### Existing HTTP-audited approvals: materialize only

```bash
.venv/bin/python -m scripts.domain_spec_operator materialize-approved \
  --base-spec /absolute/operator/complete-eie-base.yaml \
  --tenant "$L9_TENANT_ID" --domain "$L9_TENANT_ID" \
  --node-label "$L9_TARGET_NODE_LABEL" \
  --batch-run-id "$L9_APPROVAL_BATCH_ID" \
  --output /absolute/operator/review/materialized.yaml
```

The command uses existing `pg_store.get_approved_schema_proposals(tenant_id, domain)`. Domain must explicitly equal tenant; node target is required. Repeat `--batch-run-id` for selected approved batches, or omit it to inspect all approved rows. Review timestamps and names must be present, identities must match, and rejected/pending rows are not used. A selected nonexistent/nonapproved batch is refused. Already-present same-type fields are skipped without another version bump, preventing double replay. Existing conflicting types fail unless `--allow-overwrite` is explicitly passed. This command only creates a candidate; it does **not** publish or fabricate a full-artifact approval. Source audit review names are not substituted for separate exact-byte publication review.

### Library seam: materialize, review exact bytes, durable audit then publish

```python
candidate_bytes = materialize_approved_spec(
    complete_base, proposals, explicit_decisions,
    tenant_id=tenant, node_label=target_node,
)
# A real named reviewer reviews these final bytes and supplies review evidence.
receipt = await approve_and_publish(
    operator_root, tenant, complete_base, proposals, explicit_decisions,
    node_label=target_node,
    batch_run_id=explicit_batch_id,
    reviewed_input_sha256=reviewed_final_candidate_digest,
    reviewed_by=explicit_named_reviewer,
    approval_ref=explicit_evidence_reference,
    expected_previous_sha256=observed_previous_digest,
)
```

`approve_and_publish` verifies the final materialized candidate and exact reviewed digest **before** database persistence. It persists every explicitly decided proposal (including rejection audit, excluding undecided fields) via the existing `persist_schema_proposal_batch` transaction, with no gate/scoring audit payload. Only after that call completes may publication occur. A failed audit prevents publication. An all-rejected/no-applied decision set may be audited but returns `None` and never changes the pointer. There is no new database schema, migration or approval authority.

## 3. Publish a separately reviewed full artifact

The operator reviews `/absolute/operator/review/materialized.yaml` and obtains an actual named reviewer and evidence reference. Compute the digest of that final unchanged file:

```bash
sha256sum /absolute/operator/review/materialized.yaml
.venv/bin/python -m scripts.domain_spec_operator publish \
  --root /absolute/operator/isolated-eie-store \
  --tenant "$L9_TENANT_ID" \
  --spec /absolute/operator/review/materialized.yaml \
  --reviewed-input-sha256 "$L9_REVIEWED_FINAL_SHA256" \
  --reviewed-by "$L9_NAMED_REVIEWER" \
  --approval-ref "$L9_APPROVAL_REFERENCE" \
  --approval-batch "$L9_APPROVAL_BATCH_ID" \
  --expected-previous-sha256 "$L9_OBSERVED_PREVIOUS_SHA256"
```

For first publication only, omit `--expected-previous-sha256`: `None` means the store must have no current pointer. For an update, the prior digest is mandatory in practice: absence refuses any existing current revision. Exact duplicate current publication with matching original review evidence is safely idempotent and returns the original receipt. Conflicting reviewer/reference/batch evidence cannot rewrite an existing receipt. The publisher never promotes a draft or deploys/mounts anything.

After isolated publication, a fresh `DomainYamlReader(root, require_published=True).load(tenant)` must expose the expected `version`, `source_sha256`, `approval_ref` and preserved `raw_spec`. The effective registry, enrichment handlers and packet router should agree on that published revision; database rows are not added again. This is local filesystem proof, not production-mount authorization.

## 4. Failure recovery and provenance-preserving rollback

On stale CAS, do not replace the expected hash blindly. Inspect the changed effective spec, reconcile the competing change, materialize a new candidate and obtain review of its final bytes. The previous pointer remains unchanged when validation or revision installation fails. A failure after a complete revision is installed but before pointer replacement can leave a detached approved revision; retrying the identical reviewed bytes with original evidence safely reuses it. Partial staging directories are never effective and normal failures clean them up. An abrupt process/power failure may leave hidden staging files; do not activate them or delete approved history.

Filesystem and database transactions cannot be one distributed atomic transaction. If publication fails after the audit commit, audit evidence may exist while the prior YAML remains effective. That is intentional: DB is audit only. Retry the same reviewed candidate/batch after inspecting current state. A directory fsync failure after pointer replacement can make completion/durability uncertain; inspect the pointer and reader result, then perform an idempotent retry rather than declaring rollback or success from the exception alone.

Rollback takes **existing** approved revision bytes and the **original** receipt, with no new reviewer, reference or timestamp:

```bash
.venv/bin/python -m scripts.domain_spec_operator rollback \
  --root /absolute/operator/isolated-eie-store \
  --tenant "$L9_TENANT_ID" \
  --sha256 "$L9_PREVIOUS_APPROVED_SHA256" \
  --expected-previous-sha256 "$L9_OBSERVED_CURRENT_SHA256"
```

Tampered/missing target bytes or receipt and stale expected pointer fail closed. No new approval is synthesized. This EIE-only rollback must be paired with compatible CEG rollback/revalidation before any separately authorized coordinated release; restoring just one artifact is not proof that shared sync vocabulary still matches.

## Separate writer/action inventory and explicit holds

| Existing surface | Observed reachability / behavior | Native lifecycle disposition |
|---|---|---|
| `app/services/workers/schema_promotion_worker.py::SchemaPromotionWorker` | Exported by worker/services packages; no constructor/start wiring found in `app/` or `scripts/`. If started externally, Redis `schema.field.discovered` thresholds write `config/domain_kb/<domain>/schema.yaml`, retain sample values, and emit `schema.field.promoted`. | **HOLD outside effective-spec supply chain.** A mutable threshold-only KB writer lacks explicit named artifact approval, native validation, exact digest, receipt and CAS. Do not point it at the published root, interpret its event as approval, or wire it into publication. The worker file is unchanged; this runbook does not disable an independently deployed instance or claim a deployment inventory was inspected. |
| `app/services/chassis_handlers.py::handle_schema_proposal` / SDK `schema-proposal` | Supplemental registration acknowledges `status: received`, tenant and field count only. It does not persist audit rows, review, validate or publish. | **Acknowledgement-only/no-op for approval and publication.** Do not treat receipt of this action as durable decision evidence. Existing explicit audit APIs plus this offline workflow remain the path; no new production route is introduced. |
| PostgreSQL approved records | Existing transactional audit and offline candidate materialization. | **Audit only.** Native runtime must not replay them after loading the complete published spec. |

These holds reconcile ownership rather than deleting discovery, scanner/seed, durable audit, existing actions or a separately scoped KB workflow.

## Validation and residual limits

Use narrow diagnostics without bypassing the repository's mandatory full release gates:

```bash
.venv/bin/pytest tests/unit/test_native_domain_workflow.py \
  tests/integration/test_native_domain_publication.py \
  tests/test_schema_proposer.py \
  tests/contracts/tier2/test_schema_proposer_behavioral.py --no-cov -q
.venv/bin/ruff check app/services/domain_spec_workflow.py \
  scripts/domain_spec_operator.py app/engines/convergence/schema_proposer.py \
  tests/unit/test_native_domain_workflow.py tests/integration/test_native_domain_publication.py
```

Tests exercise isolated local stores and mocked PostgreSQL audit seams: exact review digest, first/update CAS, concurrent writers, idempotent retry, partial failure, retained history, original-receipt rollback, tamper, symlinks, seed refusal, redaction/ambiguity, explicit targets, preserved hints/rules/resolution and rejected/undecided fields. They are not proof of real PostgreSQL availability, operator authorization, real production read-only mounts, cryptographic authenticity or a live release. No commit is authorized by narrow diagnostics; repository mandatory gates remain the parent/operator's responsibility before any future authorized commit.
