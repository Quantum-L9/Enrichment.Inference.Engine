# Domain identity registry contract

> **Source:** `app/engines/domain_yaml_reader.py` (grammar, reader), `app/api/v1/converge.py` (registry, approvals), `app/api/v1/discover.py` (scan parity), `app/services/pg_store.py` (`persist_schema_proposal_batch`, `get_approved_schema_proposals`), `app/main.py` (startup, readiness).
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
| Single source | `app.engines.domain_yaml_reader.is_legal_tenant_id`. The reader applies it to every id that reaches the filesystem; the registry (`app.api.v1.converge`) imports the same function. There is no second regex. |

Vertical templates are provenance only (`metadata.template.id` / `metadata.template.version`). There is no tenant-to-domain mapping layer anywhere in EIE.

## 2. Registry

`converge._domain_specs` is the one in-process registry, keyed by tenant id. `build_domain_spec_registry(reader)` fills it at startup from the domains root (`Settings.domains_dir`, default `./domains`):

| Directory under `domains/` | Registered? | Log event |
|---|---|---|
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

A routable node with no domain spec can serve no tenant's converge or scan, so it is not Ready. `ReadinessResponse` is a T5 schema and carries no registry field; the state is in the status code and in the startup log line `converge_module_configured` (`domains`, `domain_registry`, `approved_revisions`).

## 3. Unknown domain — one resolution path, one status

`converge.get_domain_spec(domain)` is the only way a scan resolves a spec. `POST /v1/scan` and `POST /api/v1/scan` both call it, so an unregistered domain answers **HTTP 404** on both, with the same detail (`Domain '<id>' not found. Available: [...]`). 404 is the status `docs/contracts/api/openapi.yaml` documents for `/api/v1/scan`.

## 4. Approved spec revisions

`POST /v1/converge/{run_id}/approve` is the write side; the `schema_proposals` table is the durable record of a domain's revisions.

1. **Serialised per domain.** An `asyncio.Lock` per tenant id is held across propose → apply → persist → publish, so a second approval for the same domain proposes against the revision the first one published, never against the snapshot both started from. Two concurrent approvals keep both field sets.
2. **Durable before published.** Every decided field (approved and rejected — rejections are the audit trail) is written through `pg_store.persist_schema_proposal_batch` in **one transaction**: one session, one `begin()`, upsert on `(tenant_id, domain, batch_run_id, field_name)` with `approval_status`, `reviewed_by`, `reviewed_at` stamped in the same transaction. A failure on any row rolls the whole batch back and the endpoint answers **503** without touching the registry. `tenant_id == domain`; `batch_run_id` is the run id.
3. **Replayed at startup.** After the registry is built, `hydrate_approved_revisions` reads `get_approved_schema_proposals(tenant_id=domain, domain=domain)` for every registered tenant and re-applies the rows batch by batch (`apply_approved_records`); each `batch_run_id` is one revision and one version bump. A store that is unreachable at startup leaves the file-backed revision in place and stops the walk (`schema_revision_hydrate_unavailable`); durability is enforced where the approval is written, not where it is read.
4. **Both spec shapes.** `schema_proposer.apply` writes the list-shaped ontology the live specs carry (`nodes: [{label, properties: [{name, type}]}]`) and the dict shape, each in its own shape.

## 5. Out of scope

- The CEG `sync` endpoint a tenant's rows are addressed to: `docs/contracts/dependencies/constellation-gate.yaml` (GRAPH-IDENT-02).
- Neo4j database provisioning: owned by Cognitive.Engine.Graphs; EIE holds no graph driver.
