# Domain identity registry contract

> **Source:** `app/engines/domain_yaml_reader.py` (grammar, spec identity, reader), `app/api/v1/converge.py` (registry, direct-access posture, approvals, scan), `app/api/v1/discover.py` (scan parity), `app/services/pg_store.py` (`persist_schema_proposal_batch`, `get_approved_schema_proposals`), `app/main.py` (startup, readiness).
> **Version:** 1.0.0 · **Effective:** 2026-10-22 (operator decision, plan dac62dc3)
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

`converge._domain_specs` is the one in-process registry, keyed by tenant id. `build_domain_spec_registry(reader)` fills it at startup from the domains root (`Settings.domains_dir`, default `./domains`):

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

- **Direct tenant access.** The X-API-Key authenticates the caller but binds it to no tenant (one key hash serves the deployment). `converge.require_direct_domain_access` therefore serves a direct, tenant-scoped spec read or revision only where `L9_ENVIRONMENT` is `local`, `dev` or `test` and answers **403** everywhere else, before any run, spec or registry listing is read. The rule has one owner, `app.core.auth.ensure_direct_tenant_read_allowed` (`DIRECT_TENANT_READ_ENVIRONMENTS`, EIE-SEC-03), which `require_direct_tenant_read` also applies, so `/v1/scan` and `/api/v1/scan` answer the same status in every environment. It guards both scan routes (through `get_domain_spec`), `POST /v1/converge/{run_id}/approve` and `GET /v1/converge/proposals/{domain}`. Tenant identity outside those runtimes arrives through Gate ingress.
- **Unknown domain.** An unregistered domain answers **HTTP 404** on both scan routes, with the same detail (`Domain '<id>' not found. Available: [...]`). 404 is the status `docs/contracts/api/openapi.yaml` documents for `/api/v1/scan` and `/v1/scan`.
- **Scan identity.** The scan's `domain_id` is the registry key and its `domain_version` the declared version, so two tenants sharing one ontology scan to distinct ids and `scan_hash` values.

## 4. Approved spec revisions

`POST /v1/converge/{run_id}/approve` is the write side; the `schema_proposals` table is the durable record of a domain's revisions.

1. **Serialised per domain.** An `asyncio.Lock` per tenant id is held across propose → apply → persist → publish, so a second approval for the same domain proposes against the revision the first one published, never against the snapshot both started from. Two concurrent approvals keep both field sets.
2. **Durable before published; no revision without an applied field.** Every decided field (approved and rejected — rejections are the audit trail) is written through `pg_store.persist_schema_proposal_batch` in **one transaction**: one session, one `begin()`, upsert on `(tenant_id, domain, batch_run_id, field_name)` with `approval_status`, `reviewed_by`, `reviewed_at` stamped in the same transaction. A failure on any row rolls the whole batch back and the endpoint answers **503** without touching the registry. `tenant_id == domain`; `batch_run_id` is the run id. A batch that applies no field — every decision rejected, every approval naming a field the run did not propose, or only non-canonical names — publishes no revision and bumps no version (`schema_proposer.apply`, `applied_field_names`), because startup replays approved rows only and could never reproduce that bump. The response reports applied fields (`approved`), rejecting decisions (`rejected`), approvals that applied nothing (`unapplied`), rows written (`persisted`) and the registry's version after the approval (`new_version`).
3. **Replayed at startup.** After the registry is built, `hydrate_approved_revisions` reads `get_approved_schema_proposals(tenant_id=domain, domain=domain)` for every registered tenant and re-applies the rows batch by batch (`apply_approved_records`); each `batch_run_id` that applies a field is one revision and one version bump, exactly as it was live. A store that is unreachable at startup stops the walk (`schema_revision_hydrate_unavailable`), leaves that tenant at its file-backed revision and raises `SchemaRevisionHydrateError`; `app.main` records the registry as `hydrate_failed` and readiness answers 503 (§2), so a node serving a rolled-back revision is never Ready.
4. **Both spec shapes.** `schema_proposer.apply` writes the list-shaped ontology the live specs carry (`nodes: [{label, properties: [{name, type}]}]`) and the dict shape, each in its own shape.
5. **The API registry only.** An approval revises the registry entry (`converge._domain_specs`): its properties, gates, scoring and version. The converge handlers read the file-backed revision through `DomainYamlReader` (rules, hints, version); approvals do not revise them.

## 5. Out of scope

- The CEG `sync` endpoint a tenant's rows are addressed to: `docs/contracts/dependencies/constellation-gate.yaml` (GRAPH-IDENT-02).
- Neo4j database provisioning: owned by Cognitive.Engine.Graphs; EIE holds no graph driver.
