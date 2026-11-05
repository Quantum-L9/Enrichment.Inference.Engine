# Schema proposal audit ownership — contract version 1

## Authority and scope

F002 Option A binds every new schema proposal audit write to the ontology node recorded during discovery and the SHA-256 digest of the retained discovery-spec bytes. The ledger is audit evidence, not effective runtime authority: only the separately reviewed immutable complete YAML supplies effective configuration. An approval-time destination, object type, provider/source attribution, current sole node, or matching field name cannot create missing historical ownership. Authentication and authorization remain with existing callers; this persistence change adds no permission or deployment.

The owned implementation is `app/services/pg_models.py`, `app/services/pg_store.py`, and the **unexecuted** migration `migrations/versions/003_schema_proposal_node_ownership.py`. Revision `003` follows the verified `002 → 001` lineage. No convergence-run SQL schema change is included.

## Binding and durable identity

| Column | ORM/SQL shape | Meaning |
|---|---|---|
| `node_label` | nullable `str` / `VARCHAR(128)` | Discovery-recorded ontology identifier matching `^[A-Za-z_][A-Za-z0-9_]*$`; case-sensitive. No default owner. |
| `discovery_spec_sha256` | nullable `str` / `VARCHAR(64)` | Exactly 64 lowercase hexadecimal characters, identifying original discovery bytes. No default digest. |
| `ownership_contract_version` | nullable `int` / `INTEGER` | `NULL` for historical-unbound; exactly integer `1` for new bound records. Server default `1` applies only to subsequent inserts. |

A bound row is uniquely identified by `(tenant_id, domain, batch_run_id, node_label, field_name)`. New writes require a legal EIE tenant id equal to the domain. A field name must satisfy the existing proposer canonical-name validator, including the kernel drift-name refusal list, and fit the existing 256-character field column. Batch and reviewer identifiers must be nonempty, unpadded text of at most 256 characters.

Every new write must explicitly supply all three binding values. A missing or malformed binding is refused; the store does not verify discovery artifact bytes or ontology membership, which remain producer/consumer obligations. `approved` is a strict boolean, not an integer or truthy string. Required field type/source values fit their existing columns. Rates are finite numbers between zero and one and are normalized to the existing `Numeric(5, 4)` precision. Samples and distributions must be finite JSON containers; inputs are snapshotted before opening a session. Optional gate/scoring/YAML-diff columns are text or `None`.

## Public persistence interfaces

`save_schema_proposal` retains its original positional arguments and optional payload defaults, adding these keyword-only arguments:

```python
node_label: str | None = None
discovery_spec_sha256: str | None = None
ownership_contract_version: int | None = None
```

These defaults exist for a stable explicit refusal of legacy callers, **not** for permission to persist unbound proposals. The method returns `SchemaProposalRecord`, inserts bound pending evidence, and accepts only an identical pending retry. A pending save cannot reset or replace a reviewed decision.

`persist_schema_proposal_batch(*, tenant_id, domain, batch_run_id, reviewed_by, decisions)` retains its existing call signature. Every decision mapping carries `field_name`, `field_type`, `source`, `fill_rate`, `avg_confidence`, `node_label`, `discovery_spec_sha256`, `ownership_contract_version=1`, and `approved`, plus optional samples/distribution/gate/scoring/YAML-diff columns. Optional row-level tenant/domain/batch/reviewer assertions, if present, must equal the outer context. Duplicate `(node_label, field_name)` pairs fail; identically named fields on different nodes are distinct. The **entire batch** is validated before a session opens. An empty batch still validates scope and reviewer and returns `[]` without opening a session.

The batch method returns `list[SchemaProposalRecord]` in incoming decision order. One transaction covers all rows. It can explicitly review a bound pending record only if its immutable evidence matches. A bad later row or database exception rolls back the whole batch. Neither method changes existing evidence when a retry conflicts.

`approve_schema_proposal(proposal_id: UUID, reviewed_by: str, approved: bool) -> SchemaProposalRecord` locks and loads the existing UUID row before any update. Missing records raise `SchemaProposalNotFoundError("Schema proposal not found")`, which is both a `ValueError` and a `LookupError` so existing callers and the HTTP 404 mapping both match; historical-unbound records raise the binding refusal below. Only bound pending evidence can transition to approved/rejected. An exact reviewed retry returns the original record. A conflicting status or reviewer is refused; the endpoint's existing authorization posture is unchanged.

## Shared consumer validator and read DTOs

```python
validate_schema_proposal_binding(record: Any) -> tuple[str, str]
```

The helper accepts mapping DTOs and attribute/ORM objects and returns the recorded `(node_label, discovery_spec_sha256)`. It does not accept an operator owner argument or infer any value. All three absent/`None` binding fields always raise exactly:

```text
Schema proposal is legacy-unbound
```

Partial binding is refused with `ValueError` describing the invalid version, identifier, or digest. Consumers must additionally verify tenant/domain, canonical field names, retained discovery-artifact identity and node membership under their own contract before candidate materialization or replay.

`get_pending_schema_proposals(tenant_id, domain)` and `get_approved_schema_proposals(tenant_id, domain)` return full `SchemaProposalRecord` objects including all three nullable binding columns. Both retain unbound rows; no ownership-version filter hides historical evidence. Pending results retain descending-confidence order. Approved results are ordered by creation timestamp, node label, and field name. Consumers must refuse an explicitly selected or all-history batch containing unbound evidence rather than silently treat a filtered history as complete. Recovery requires fresh owner-bound discovery and review with a new batch id; it never mutates or restamps historical rows.

## Immutable retries and concurrency contract

Immutable payload includes tenant/domain/batch/node/field identity, discovery digest/version, field type, source, normalized rates, samples, distribution, gate, scoring dimension and YAML diff. The review status and reviewer must also match an already reviewed retry. Original UUID, creation time, review time and reviewer are retained; a different reviewer does not overwrite earlier review attribution.

Persistence emits PostgreSQL `INSERT ... ON CONFLICT (tenant_id, domain, batch_run_id, node_label, field_name) WHERE ownership_contract_version = 1 DO NOTHING`, followed by an owner-qualified `SELECT ... FOR UPDATE` with ORM identity-map refresh. The partial-index predicate is constant SQL, not a parameterized predicate requiring index inference from an unknown bind. Only the reloaded winner is compared; incoming evidence never silently overrides it. Mixed-owner batches acquire locks in sorted pair order and return results in caller order. A conflict raises `ValueError` prefixed by `Conflicting schema proposal retry:`. Pending-to-reviewed transitions occur only through the two explicit review methods.

## Migration and rollback safety

Upgrade adds three nullable columns without defaults first, installs an explicit check permitting either **all three NULL** or **all three explicitly non-NULL and valid version 1**, creates bound and legacy partial unique indexes and the owner query index, drops the old field-only unique constraint, and finally installs version default `1`. The normal old-code insert without binding then fails the check instead of fabricating new unbound evidence. No historical binding is fabricated and no review/status/time is updated.

The bound partial unique index is `uq_schema_proposal_batch_node_field` on `(tenant_id, domain, batch_run_id, node_label, field_name)` where version is `1`. The legacy partial unique index is `uq_schema_proposal_legacy_batch_field` on `(tenant_id, domain, batch_run_id, field_name)` where version is `NULL`. The owner query index is `ix_schema_proposals_owner_status_created` on `(tenant_id, domain, node_label, approval_status, created_at)`. ORM metadata matches the check, defaults and indexes.

Downgrade obtains a transaction-scoped PostgreSQL `ACCESS EXCLUSIVE` table lock (not schema DDL), then preflights bound evidence and old-key collisions **before any schema DDL**. Any non-NULL ownership evidence or duplicate old `(tenant, domain, batch, field)` key raises `RuntimeError` prefixed by `Cannot downgrade schema proposal ownership:`. Only an empty-of-bound-records, collision-free ledger can restore the old unique key and remove the new schema. There is no deletion, deduplication or archival bypass. A later authorized rollout must run under Alembic's transactional PostgreSQL context with old writers quiesced and backups retained.

## Validation boundary

Unit tests compile PostgreSQL SQL and simulate insert conflicts, locked reloads, identical/conflicting retries, immutable reviews, full-batch rollback, pre-session refusals and retained legacy reads. Migration contract tests inspect AST/ORM metadata and import the artifact against a fake Alembic operation object to capture SQL and preflight refusal. They never connect to a database. These tests **do not prove** actual PostgreSQL index enforcement, concurrent transaction behavior or migration execution. A real isolated PostgreSQL witness and database rollout remain separately authorized follow-up work. No database migration, deployment, effective-spec activation or historical approval occurred during this implementation.
