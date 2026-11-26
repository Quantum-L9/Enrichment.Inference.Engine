# Domain identity registry contract

> **Source:** `app/engines/domain_yaml_reader.py` (grammar, spec identity, reader), `app/api/v1/converge.py` (registry, direct-access posture, approvals, scan), `app/api/v1/discover.py` (scan parity), `app/services/pg_store.py` (`persist_schema_proposal_batch`, `get_approved_schema_proposals`), `app/main.py` (startup, readiness).
> **Version:** 2.0.0 candidate · **Local execution decision:** 2026-11-03 (native-v3; no deployment admission claimed)
> **Pinned by:** `tests/contracts/test_domain_registry_contract.py`

---

## 1. Identity

One customer is one tenant id, one directory `domains/<tenant>/spec.yaml`, one versioned spec and one Neo4j database. The three names are the same string:

```text
tenant id == domain spec id == Neo4j database name
```

A tenant id is therefore a legal Neo4j database name:

| Rule | Value |
|------|-------|
| Grammar | `^[a-z][a-z0-9.-]{2,62}$` — lowercase letter first, then lowercase letters, digits, dots or dashes, 3–63 characters |
| Case | lowercase only; a mixed-case id is refused, never lowercased into a different identity |
| Reserved | an id starting with `system` is refused |
| Single source | `app.engines.domain_yaml_reader.is_legal_tenant_id`. The reader applies it to every id that reaches the filesystem; the registry (`app.api.v1.converge`) imports the same function. `app.models.schemas.validate_tenant_id` (and `app.core.auth.tenant_scope`, which calls it) applies the reader's `TENANT_ID_PATTERN` and `TENANT_ID_RESERVED_PREFIX` and only adds the reason a value was refused. There is no second regex. |

Vertical templates are provenance only (`metadata.template.id` / `metadata.template.version`). There is no tenant-to-domain mapping layer anywhere in EIE.

### Declared spec identity

`app.engines.domain_yaml_reader.declared_spec_identity(spec)` is the one reader of the `(id, version)` a spec declares. The registry checks the id against the directory, `DomainYamlReader` reports the version, and a registry scan stamps the version onto its result.

| Source, highest precedence first | id | version |
|---|---|---|
| top level (the live specs) | `name` | `version` (the field an approval bumps) |
| `domain_identity` block | `id`, else `name` | `version` |
| `domain` string or block | the string, or `id`, else `name` | `version` |

`metadata` (template provenance) is never identity.

## 2. Registry

`converge._domain_specs` is the one in-process registry, keyed by tenant id. `build_domain_spec_registry(reader)` returns a read-only `EffectiveDomainRegistry` backed by the same reader injected into handlers and packet routing. Every spec read resolves the reader's effective snapshot; no API-side copy can override published YAML. Unsigned local/dev/test may still use directory examples under `Settings.domains_dir` (default `./domains`). Tenant-serving staging/prod requires immutable native publication and never implicitly activates those examples.

Only the directory layout `domains/<tenant>/spec.yaml` names a tenant.

| Entry under `domains/` | Registered? | Log event |
|---|---|---|
| a flat file `<name>.yaml` / `<name>.yml` (e.g. `acme.yaml` beside `acme/`, a stray `acme.bak.yaml`) | no — never a tenant, never shadows a directory | `domain_registry_refused_flat_spec` (`file`) |
| legal tenant id, readable spec whose declared `name` (or `domain` / `domain_identity.id`) equals the directory name, or declares none | yes | `domain_registry_built` lists it |
| name fails the grammar (`Metals`, `ab`, `under_score`, `systemwide`) | no | `domain_registry_refused_illegal_tenant_id` |
| spec declares a different id than its directory | no | `domain_registry_refused_identity_mismatch` |
| unreadable, malformed YAML, or valid YAML of the wrong shape (list root, `ontology.nodes` as a mapping) | no — that tenant only; the walk continues | `domain_registry_spec_unreadable` |

One broken tenant refuses that tenant. It does not fail the registry.

### Readiness

`app.main._domain_registry_state` records the outcome of the load and `GET /api/v1/ready` reads it:

| State | Meaning | `/api/v1/ready` |
|-------|---------|-----------------|
| `not_attempted` | startup has not run (uvicorn serves no request before lifespan startup) | not degraded |
| `loaded` | at least one tenant spec registered | 200 (subject to Gate registration) |
| `empty` | the domains root yielded no legal spec | **503** |
| `failed` | the load itself raised | **503** |
| `hydrate_failed` | the registry loaded, but the approved revisions could not be read back (§4.3): at least one tenant would be served its rolled-back file revision | **503** |

A routable node with no domain spec can serve no tenant's converge or scan, so it is not Ready. `ReadinessResponse` is a T5 schema and carries no registry field; the state is in the status code and in the startup log line `converge_module_configured` (`domains`, `domain_registry`, `approved_revisions`).

## 3. One resolution path, one posture, one status

`converge.get_domain_spec(domain)` is the only way a scan resolves a spec; `converge.scan_registered_domain` wraps it for both `POST /v1/scan` and `POST /api/v1/scan`, so:

- **Direct tenant access.** The X-API-Key authenticates the caller but binds it to no tenant (one key hash serves the deployment). `converge.require_direct_domain_access` therefore serves a direct, tenant-scoped spec read or revision only where `L9_ENVIRONMENT` is `local`, `dev` or `test` and answers **403** everywhere else, before any run, spec or registry listing is read. The rule has one owner, `app.core.auth.ensure_direct_tenant_read_allowed` (`DIRECT_TENANT_READ_ENVIRONMENTS`, EIE-SEC-03), which `require_direct_tenant_read` also applies, so `/v1/scan` and `/api/v1/scan` answer the same status in every environment. It guards both scan routes (through `get_domain_spec`), `POST /v1/converge/{run_id}/approve`, `GET /v1/converge/proposals/{domain}`, `GET /v1/converge/{run_id}` (EIE-002, before the run is loaded), `POST /v1/converge/batch` whenever it names a `domain` (EIE-008) and, as a route dependency, `POST /api/v1/proposals/{proposal_id}/approve` (EIE-001). `tests/unit/test_audit_tenant_boundary.py` holds every X-API-Key operation of the mounted app to this posture unless it is a recorded exemption. Tenant identity outside those runtimes arrives through Gate ingress.
- **Unknown domain.** An unregistered domain answers **HTTP 404** on both scan routes, with the same detail (`Domain '<id>' not found. Available: [...]`). 404 is the status `docs/contracts/api/openapi.yaml` documents for `/api/v1/scan` and `/v1/scan`.
- **Scan identity.** The scan's `domain_id` is the registry key and its `domain_version` the declared version, so two tenants sharing one ontology scan to distinct ids and `scan_hash` values.

## 4. Approved spec revisions: one effective authority

Immutable published EIE YAML is the effective runtime authority. `schema_proposals` remains the durable human-decision audit ledger, not a competing spec assembled at startup. The operator workflow reuses the scanner, partial seed generator, proposer and ledger. Seeds are never automatically live; complete native validation and explicitly supplied review coordinates are required for publication.

The node's HTTP approval route retains the local/dev/test-only authorization posture. Every decided proposal is persisted through `pg_store.persist_schema_proposal_batch` in one transaction before reporting an approval. On a reader-backed runtime registry the route returns `publication_status: awaiting_operator_publication`; it cannot mutate a read-only operator mount. `new_version` remains the actual effective version until an operator publishes reviewed complete bytes. Rejected or unapproved fields do not enter the materialized YAML. `schema_proposer` writes enrichment fields only; CEG owns gates and scoring.

An effective revision uses `<root>/<tenant>/current.json` to select `<root>/<tenant>/revisions/<sha256>/spec.yaml`, alongside `receipt.json`. The native approval receipt binds tenant, version, exact artifact hash, exact reviewed-input hash, explicit reviewer, approval reference, timestamp and previous revision. The content-addressed file and receipt remain immutable; publication atomically replaces the current pointer after validation. A hash proves byte equality, not human authenticity; trusted operator controls and read-only permissions remain independent requirements.

`DomainYamlReader` validates the complete EIE-native structure, the pointer, approval receipt and bytes. It exposes source hash and approval reference and detects pointer changes, stale/tampered receipts and artifact tamper even after a warm read. Registry, hints/rules and graph-sync selection use this owner. Invalid approved artifacts raise visibly rather than reverting to legacy files or graph settings fallback. Readiness rechecks the registry's effective revisions. Restart consumes the same published YAML without replaying ledger rows, whether the approval DB is reachable or not.

Discovery records the explicit ontology `node_label` and original artifact SHA-256 before provider work. The run state retains that owner, digest, actual confidence and complete observed source sets. Proposals and decisions use `(node_label, field_name)` identities; approval cannot supply a missing historical owner or retarget a recorded owner. The durable unique identity is `(tenant_id, domain, batch_run_id, node_label, field_name)`. Same-field discoveries on different nodes remain independent; conflicting retry evidence or reviewer decisions cannot overwrite an existing audit row.

Legacy unbound runs/rows remain readable diagnostics but cannot be approved, materialized or replayed, even for a sole-node spec. The retained explicit replay helper requires original discovery bytes, complete owner binding and original review evidence. Normal startup never invokes this compatibility authority: it verifies the reader-backed registry only. Offline audited materialization verifies original discovery bytes and owner membership before intentional rebase onto a newer complete base; that new candidate still requires separate exact-byte review and publication. See `SCHEMA_PROPOSAL_NODE_OWNERSHIP.md` and `SCHEMA_PROPOSAL_AUDIT_OWNERSHIP.md`. Migration `003_schema_proposal_node_ownership` is an unexecuted artifact: it preserves historical NULL binding, performs no ownership backfill or review restamp, and refuses lossy downgrade before schema DDL.

## 4.1 Cross-engine release barrier

EIE and CEG retain independent purpose-specific schemas, versions and hashes. EIE's operator `scripts/check_domain_compatibility.py` invokes both native validation owners and checks only tenant identity and the EIE-referenced CEG sync method/path/target/idproperty plus declared shared field types and explicitly supplied field-meaning assertions. It writes two hashes and their approval references into a compatible-pair proof; one independently rolled-back hash invalidates the old pair. An absent approval is **Unknown**, not release-ready. The checker does not rename rows, merge YAML, compile schemas, or alter CEG ranking. Actual cross-store cutover and platform mount proof remain separately authorized operator work.

## 5. Out of scope

- The CEG `sync` endpoint a tenant's rows are addressed to: `docs/contracts/dependencies/constellation-gate.yaml` (GRAPH-IDENT-02).
- Neo4j database provisioning: owned by Cognitive.Engine.Graphs; EIE holds no graph driver.
- Docker/Compose isolation and production mount changes: deliberately untouched in this local candidate.
- Live deployment, production migration execution, approval fabrication, Gate spec-delivery action, Semantic Compiler prerequisite, PR merge or issue mutation. Review-PR publication is independently authorized campaign work, not runtime activation.
