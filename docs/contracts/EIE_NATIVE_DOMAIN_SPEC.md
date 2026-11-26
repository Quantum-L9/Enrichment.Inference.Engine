# EIE native DomainSpec and effective reader

**Contract version:** `eie.native-domain-spec/v2` (v2, 2026-11-11: typed property declarations shared with CEG; typed sync row contract)
**Executable owner:** `app/engines/domain_yaml_reader.py::validate_native_spec`
**Execution authority:** `native-domain-supply-chain-g27r-v3` (local source changes only)

EIE enrichment/inference YAML and CEG graph/matching YAML are distinct artifacts with independent versions and hashes. EIE does not inherit CEG's DomainSpec schema, generate graph ranking policy, or require a semantic compiler. The checked-in `domains/plasticos/spec.yaml` is an immutable authoring/test specimen, not an implicitly approved production artifact.

## Native validation

```python
from app.engines.domain_yaml_reader import validate_native_spec

validated = validate_native_spec(raw, tenant_id="plasticos")
```

The function accepts an object, raises `ValueError` for refused native structure, and returns a deep-copied `dict[str, Any]` preserving reviewed values. It does not normalize identities, rewrite YAML, add defaults, coerce fields into CEG models, or create review evidence.

The root must be a string-keyed mapping. Native identity is top-level `name` and `version`; `name` must equal the requested tenant. The existing shared Neo4j/tenant grammar is unchanged: lowercase, a leading letter, then letters/digits/dots/dashes, 3–63 characters, excluding the reserved `system` prefix. Legacy `domain`/`domain_identity` blocks are not native identity authority. `version` must be a semantic-version string, including optional prerelease/build identifiers; numeric prerelease identifiers cannot have leading zeroes. Numeric YAML versions and booleans are refused.

`ontology.nodes` is a nonempty list of node mappings with unique identifier-shaped `label` values. Each node has a nonempty list of property mappings with unique identifier-shaped `name` values and an explicit `type` in the typed declaration syntax EIE and CEG share (`app/engines/spec_types.py`, owner decision 2026-11-11):

```yaml
- {name: certifications, type: list, items: {type: string}}
- name: processed_polymers
  type: list
  items:
    type: map
    properties:
      - {name: code, type: string, required: true}
- name: location
  type: map
  properties:
    - {name: lat, type: float}
```

Scalar types are CEG's `string`, `int`, `float`, `bool`, `datetime`, `date`, `duration` and `enum` (optional nonempty string `values`); the EIE-native spellings `integer`, `boolean` and `number` remain admitted for EIE-only properties, but a property that rides a sync row must use CEG's spelling (eie-ceg-sync/v2 compares the literal type). `items` is required iff the type is `list` (a scalar, enum or map; never a list of lists) and `properties` is required iff it is `map`; `type: list` without `items` is refused, and the untyped containers `dict` and `object` are refused (declare a `map`). Optional `required` is a boolean. Optional `evidenceonly: true` marks an EIE enrichment, inference or resolution input that is never projected onto a sync row and is not part of the row contract. Native properties are not a CEG-style mapping or legacy string list. Optional `queryschema.fields` uses the same property-list checks.

Optional node `enrichment_hints` is a mapping. Empty hints are allowed; nonempty hints require a nonblank `objective_template`. Supported keys are `objective_template`, `priority_fields`, `kb_context`, `refresh_interval_days`, `min_confidence`, `max_variations_discovery`, and `max_variations_targeted`. Priority fields must be unique declarations on that node. `kb_context` is a nonblank string or null. Confidence must be finite and within `[0, 1]`, excluding booleans. Refresh days and variation limits are positive integers.

Optional `inference_rules` is a list of uniquely identified rules. Each rule requires nonempty conditions and outputs, a nonblank `rule_id`, nonblank `rule_or_model_version` provenance, and finite `[0, 1]` confidence. Priority is an integer in `[0, 100]` when present. An optional rule domain equals the tenant. Condition/output fields must be declared in the EIE ontology. Existing EIE `RuleDefinition` validation owns operator and output semantics: `CONTAINS`, `EQUALS`, `GT`, `LT`, `GTE`, `LTE`, `IN`, `NOTIN`, `ISTRUE`, `ISFALSE`, and `EXISTS`; output derivations are `classification`, `computation`, `lookup`, and `transform`. Validation preserves the original dictionaries rather than model-coerced output.

Drafts may instead declare an `inference_rules_path` using a bounded relative `.yaml`/`.yml` path with safe alphanumeric/dot/dash/underscore components. Absolute paths, URLs, backslashes, empty components, `.`/`..`, and control characters are refused. Nonempty inline rules and a reference cannot coexist. **Published artifacts must contain inline rules:** no referenced external file is admitted as effective authority. An operator must materialize and review those rules inside the exact published YAML bytes. On a legacy (unpublished) snapshot, the converge loader resolves a relative reference against the directory of the spec file that declares it (`DomainEnrichmentConfig.source_dir`), never against the process working directory. A declared file that cannot be read is passed on as its error text, so `InferenceBridge` reports `invalid_rules` naming that path, never `not_configured`.

Optional `semantic_registry` is a mapping with a boolean `enabled` when supplied. `canonical_entities` is a list of unique declared EIE node types. Each entry declares a resolution strategy (`exact`, `fuzzy`, `semantic`, or `hybrid`), finite `[0, 1]` resolution threshold, and nonempty unique key fields declared on that node.

Optional `sync.endpoints` is a nonempty list of mappings with unique local `/v1/sync/<endpoint>` paths and identifier-shaped `targetnode`/`idproperty`. Optional `batchstrategy` is `UNWINDMERGE`, optional `method` is `POST` or `PATCH`, and optional `childsync` (`field`, `targetnode`, `targetid`, `edgetype`, `edgedirection` of `parenttochild`/`childtoparent`) and `taxonomyedges` (`field`, `edgetype`, `targetlabel`, `targetid`) take CEG's `SyncEndpointSpec` shapes. Sync target and identifier vocabulary belongs to CEG. When the endpoint's `targetnode` is declared in this ontology, its properties that are not `evidenceonly` are the endpoint's typed **row contract**: the id property must be a declared scalar; a `map` or `list<map>` is a wire-only shape valid only as the `field` of one of the endpoint's childsync entries, whose items must declare that entry's `targetid`; a taxonomy-edge field must be a scalar or a list of scalars. `packet_router` builds every sync row from exactly that declaration (see `docs/contracts/dependencies/constellation-gate.yaml` `sync`). An endpoint whose target node this ontology does not declare has no row, and EIE sends nothing to it. The compatibility contract `eie-ceg-sync/v2` (`app/engines/domain_spec_compatibility.py`) proves, for every endpoint EIE declares, that the endpoint keys, childsync and taxonomyedges are identical to CEG's and that every property EIE can send is declared by CEG's target node with the identical typed shape (subset semantics: CEG-owned properties EIE never produces are not declared by EIE and pass).

Nonempty `gates`, `ranking`, `ranking_rules`, `matching`, `matchentities`, `match_entities`, or `scoring_dimensions` are refused at the root, node, and rule ownership surfaces. EIE `gates: []` and `scoring: {dimensions: []}` remain compatible with PlasticOS. No nonempty scoring dimensions or alternative scoring policy is admitted.

## Sync provenance

Convention of `eie-ceg-sync/v2` for the provenance of a synced value (operator
ruling 2026-11-24; the field-set rules are
`docs/contracts/data/graph_sync_intents.md` *Field set and provenance*). It is
defined here once and keyed only on declarations:

> **Status:** implemented (campaign PR-2, #420 and #421) in
> `app/engines/sync_field_set.py`, which selects and labels the field set, and
> `app/engines/handlers.py` `converge_graph_fields`. Both modes below, legacy
> and full, are live code; which one a field syncs in is chosen per field by the declarations of
> the published pack, not by an EIE release. The packs in this repository
> (`domains/plasticos/spec.yaml` and the pinned CEG snapshot
> `tests/fixtures/ceg/plasticos.spec.yaml`) declare no provenance property, so
> their rows sync in legacy mode: admitted values only, unlabelled. A deployed
> pack's mode is read from that pack, not from this note
> (`tests/contracts/test_audit_provenance_status.py`).


- A row field `F` of an endpoint's row contract carries its provenance in the
  companion row property **`F_provenance`**: a declared scalar `enum` with
  `values: [observed, enriched]`, declared on the same target node and not
  `evidenceonly`.
- Each item of a childsync `list<map>` field carries its provenance in the item
  property **`provenance`**: the same `enum`, declared among that field's item
  properties.
- `observed`: the consumer supplied the value (the request entity or a v2
  observation). `enriched`: EIE admitted it (`inferred` or `external_claim`).
  An observed value is never labelled `enriched`.
- Provenance properties are metadata, not value fields: `F_provenance` and the
  item `provenance` need no provenance of their own (there is no
  `F_provenance_provenance`). Each is sent as an ordinary declared row or item
  property alongside the value it labels, and only with it.
- Whether a value field's provenance property is declared selects its sync
  mode (CEG packs are published independently of EIE releases):

  | Mode | Provenance property | Syncs |
  |---|---|---|
  | Legacy | not declared | admitted (enriched) values only, exactly as before, unlabelled (unlabelled means enriched) |
  | Full | declared | observed and enriched values, each labelled |

  An observed value never syncs to a field whose provenance property is not
  declared.
- **Prerequisite on CEG.** CEG must declare `F_provenance` and the item
  `provenance` with the identical `enum` shape `[observed, enriched]` on the
  target node before EIE sends them; the compatibility contract then compares
  them like any other row property. The pinned CEG revision
  (`tests/contracts/ceg_revision.py`, snapshot
  `tests/fixtures/ceg/plasticos.spec.yaml`) must be moved to a revision that
  declares them before the implementation merges. CEG applies the over-time
  precedence (G3) by reading them, once they are declared.
- **`sync_version`** (`integer`, declared on the target node) is the
  per-subject monotonic version of
  `docs/contracts/data/graph_sync_intents.md` rule 6, which CEG compares to
  refuse a stale write. It is transport metadata, not a value field, and is
  independent of provenance: an endpoint may declare either, both or neither.
  Undeclared means rule 5 ordering only (legacy).

## Published filesystem layout

```text
<root>/<tenant>/
  current.json
  revisions/<sha256>/
    spec.yaml
    receipt.json
```

`current.json` has exactly this schema and a lowercase 64-character hexadecimal digest:

```json
{"schema": "eie.domain-current/v1", "sha256": "<sha256-of-exact-spec.yaml-bytes>"}
```

The per-artifact approval receipt requires:

| Field | Reader admission requirement |
| --- | --- |
| `schema` | `eie.domain-approval/v1` |
| `tenant_id` | Equals requested tenant and native `name` |
| `version` | Equals native top-level `version` |
| `sha256` | Equals current pointer, revision directory, and exact YAML byte digest |
| `reviewed_input_sha256` | Equals that same exact published byte digest; stale review input is refused |
| `reviewed_by` | Explicit nonblank caller-supplied review identity |
| `approval_ref` | Explicit nonblank caller-supplied evidence reference |
| `reviewed_at` | Parseable timezone-aware ISO timestamp, not in the future |
| `previous_sha256` | Present; null for first revision, otherwise a distinct lowercase SHA-256 digest |
| `approval_batches` | Present list of unique nonblank batch identifiers; empty is allowed |

The reader neither invents nor verifies a human's real-world authorization. Receipt structure and byte hashes are not signatures. The operator-owned publication process, approval audit, permissions, and read-only runtime mount remain the trust boundary. A structurally valid receipt substituted before a fresh reader starts cannot be authenticated from these unsigned files alone. There is no invented expiration interval or reviewer allowlist.

## Effective-reader API and failure behavior

```python
from app.engines.domain_yaml_reader import (
    DomainPublicationError,
    DomainYamlReader,
    enrichment_hints_for,
)

reader = DomainYamlReader(operator_root, require_published=True, max_cache_entries=128)
config = reader.load("plasticos")
hints = enrichment_hints_for(config, "Facility")  # no second revision read
```

`require_published` is a public read-only property. When true, a missing/non-directory or symlinked production root fails in the constructor. Publication admission rejects symlinked root ancestors or descendants, hardlinked files, nonregular files, over-limit files, unsafe pointers, unreadable/missing bytes or receipts, digest/identity/version/review mismatches, future/naive timestamps, and seed/draft-only artifacts. YAML is read with `yaml.safe_load`; JSON duplicate keys are refused. Approved `spec.yaml` bytes are read once per `load`, hashed, validated, and parsed from that same read. Pointer and receipt are reread before return; changes during admission fail closed and require an explicit caller retry.

Every `load` rechecks filesystem bytes and admission, including cache hits. SHA-256 verification is not replaced by mtime, file size, or TTL checks. The bounded LRU retains at most `max_cache_entries` parsed configurations; `invalidate(tenant)` or `invalidate()` evicts snapshots. A receipt byte change for the same revision while its cached provenance is retained is refused, including valid-looking review-reference edits. Retained provenance is never returned after failed admission. Integrity restoration permits a later fresh admission; eviction/restart does not provide historical signature verification.

If `current.json` exists (including a dangling symlink), or `require_published` is true, **there is no legacy fallback**. A reader also remembers tenants for which it has observed publication, so deleting their pointer does not silently revive `<tenant>/spec.yaml` or `<tenant>.yaml` in that reader. Fresh production readers remain protected by `require_published=True`.

`DomainPublicationError` subclasses `ValueError`; effective consumers must propagate it rather than interpreting publication refusal as “domain absent” or running an unconfigured fallback. Other owners are responsible for wiring this reader into registry, handlers, router, and readiness policy.

`DomainEnrichmentConfig` adds optional `source_sha256` and `approval_ref`. Published snapshots include both; legacy snapshots leave both null. All `load` returns are deep copies of cached configurations. `get_target_schema` and `get_enrichment_hints` return caller-owned data. `enrichment_hints_for(config, node_label)` extracts defensive hints from an existing snapshot without I/O, enabling hints and rules to come from one revision; missing labels/hints return `{}`. `default_node_label(config)` names the node a caller that supplies no `node_label` addresses: the spec's only node, else `None`. A multi-node spec has no implicit owner, and the converge loader then loads no hints and logs `handlers.converge_node_label_ambiguous`; it never substitutes a hard-coded label the spec may not define. Legacy snapshots also carry `source_dir`, the directory of the spec file read; published snapshots leave it null.

`list_directory_domains` includes tenants with either `current.json` or legacy `spec.yaml`. Listing is discovery, not approval. Legacy flat-file discovery remains available for existing diagnostics, but production load refuses it. With `require_published=False` and no observed pointer, permissive `_parse`, `_declared_id`, `_declared_version`, and `declared_spec_identity` behavior is preserved for historical nonproduction fixtures. Legacy files are rehashed on load, so mutation/deletion is no longer masked by an indefinite cache.

## Validation and delivery limits

Contract tests are `tests/contracts/test_native_domain_spec.py`; reader tests are `tests/unit/test_effective_domain_reader.py`. They cover actual PlasticOS admission, hostile schema variants, receipt/hash drift, pointer reload/restart, cached tamper, unsafe filesystem entries, same-size/restored-mtime corruption, admission races, published-reference refusal, bounded cache, and defensive snapshots.

Local diagnostic pytest uses `--no-cov` only to avoid applying the repository-wide coverage calculation to a narrow slice. This does not change or satisfy the mandatory canonical coverage gate. This implementation performs no production publication, deployment, push, PR, merge, or commit, and generates no real approval.
