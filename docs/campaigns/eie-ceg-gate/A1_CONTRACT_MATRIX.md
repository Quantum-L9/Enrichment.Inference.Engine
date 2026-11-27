# A1 — EIE / CEG / Gate typed seam contract matrix

Campaign `EIE-CEG-GATE-COLLAB-2026-11-21`, lane PROOF. This is the frozen-seam
**candidate**. The CTO freezes it at GATE G1; this document claims no authority
of its own (L9-DERIVED-001). Every row cites the source line it was read from.
Wherever a seam property is not met, the row is marked **BLOCKED** with an owner.
This document proposes no new packet field, action, payload key, spec key or
caller policy.

## 0. Pinned coordinates

| Source | Commit | Notes |
|---|---|---|
| Quantum-L9/Enrichment.Inference.Engine | `aabf855f3f027dab87a78cf09bcb3f465b1be397` | cartridge `domains/plasticos/spec.yaml` sha256 `dd09cc9e…8cfe` (v0.6.2, `current.json`) |
| Quantum-L9/Cognitive.Engine.Graphs | `9d17d01b13671c58376f1e659e34958ced445e0c` | design pack `tests/fixtures/design_packs/plasticos/spec.yaml` sha256 `825e98d1…d863` (v1.6.0) |
| Quantum-L9/Constellation.Gate | `b451b68b98b315f57f0c4f9b99090a437e9dac23` | paths under `constellation-gate/src/constellation_gate/` (`G/`) |
| Quantum-L9/Gate_SDK | head `dc4e69c4…`; **pinned by every node at `bc1c74c06d70126e82eba73c47df8f2f2af4bc2b` (v1.3.2)** | Gate `requirements.lock:10`; EIE `uv.lock:171-173`; CEG `uv.lock:179-181`. All three use tag `v1`. Head `dc4e69c4` is not a descendant of the pin, and the two differ under `src/` only in `runtime/execution.py`. |
| Quantum-L9/.github | `d0d292721046cc5e6e5083ee798aa652728d9af1` (v3.45.0) | projection `l9.projection/coding-agent-execution@1`, bundle `f8e44d1536cb` |
| TEST_ONLY CEG pack `plasticos` | `tests/fixtures/test_only_packs/plasticos/spec.yaml` sha256 `d8cbd15c339e1742032948186069827e5b1c6969a610ea9ef39aebf0ddf13863` | the design pack body verbatim, plus `plugins.inference_rules: [infer_geography_from_postal_code]` |
| TEST_ONLY CEG pack `synthneutral` (r2) | `tests/fixtures/test_only_packs/synthneutral/spec.yaml` sha256 `d50b9ab2aba2addf0c11b5c6ad3636f102f6e6479262f6b38d526d9f545bbb3e` (r1 was `6af7ad98…5a82`) | synthetic and domain-neutral, with tenant-scoped `capabilities` |
| TEST_ONLY EIE cartridge `synthneutral` | `tests/e2e/gate_loop/cartridges/synthneutral/spec.yaml` (this repo) | counterpart of the CEG synthetic pack |

The manifest is `Cognitive.Engine.Graphs tests/fixtures/test_only_packs/TEST_ONLY.json`. EIE's
snapshot `tests/fixtures/ceg/plasticos.spec.yaml` is byte-identical to the CEG design pack below its
10-line header, so the two do not drift at these heads.

## 1. Action / owner / caller table (Gate is the sole router)

Every action enters at Gate `POST /v1/execute` and is selected by `header.action` (`G/api/main.py:125`).
Owners come from `G/routing/hand_curated_owners.py:20-62`, merged into `CANONICAL_ACTION_OWNERS`
(`G/routing/action_ownership.py:215-223`).

Caller kinds are resolved from the verified `security.signing_key_id`, through
`CallerPolicy{node,kind,tenants,actions}` (`L9_KEY_ALLOWED_ACTIONS_JSON`,
`G/config/settings.py:60-138`). The checks run in this order (`G/boundary/ingress_validator.py:159-224`):
1. signed;
2. policy exists;
3. `source_node == policy.node`;
4. tenant ∈ `policy.tenants`;
5. kind rule (operator-only);
6. action ∈ `policy.actions`;
7. a consumer must not present a registered node name.

### 1a. SEAM (`action_ownership.py:35-47`)

| Action | Owner node | Allowed callers | Request keys | Response / ack | Idempotency | Errors |
|---|---|---|---|---|---|---|
| `converge` | enrichment-engine (`hco:44`) | consumer; worker by policy | EnrichRequest or request v2; `domain_id`, `node_label`, `max_passes` (EIE `app/engines/handlers.py:262-438`) | `EnrichResponse` (`schemas.py:261-305`). Failure is returned `state=failed` + `failure_reason`, never raised (`handlers.py:565-567`). | EIE Redis scope `tenant:<t>`; side-effect key `idem:{t}:{key}:converge` (`side_effect_coordinator.py:48-81`) | `result not durable` (`PersistenceRequiredError`), degraded on timeout |
| `enrich` | enrichment-engine (`hco:51`) | consumer; worker | `entity`, `object_type`, `objective` (+ `entity_id`, `schema`, `kb_context`, `idempotency_key`) | `EnrichResponse` | as converge | `ValueError` when entity identity is missing (`handlers.py:228-230`) |
| `enrich-and-sync` | enrichment-engine (`hco:52`) | consumer; worker | as enrich | enrich + `side_effects:"coordinated"` (`orchestration_layer.py:74-93`) | as enrich | as enrich |
| `graph-inference-result` | enrichment-engine (`hco:45`) | **graph worker only** (frozen seam); consumers only by policy | `inference_outputs: [{entity_id, field, value, confidence, rule, provenance, rationale}]` | `{status:"accepted", targets_queued, packet_id, tenant}` (`graph_return_channel.py:293-309`) | CEG key `ceg:graph-inference:{t}:{entity_id}:{sha256[:16]}` (CEG `engine/gate_egress.py:64-69`); **EIE intake does not dedup** | `TransportValidationError` on a bad action or a non-list body |
| `sync` | graph (`hco:23`) | enrichment-engine worker; consumer by policy | `entity_type` (last path segment), `batch` (non-empty list), optional `edge_type` (CEG `engine/handlers.py:1401-1480`) | node: `{status:"success", entity_type, synced_count=len(batch)}` + optional `undeclared_properties`, `rejected_count` (`:1605-1625`). Relationship endpoints add `created_count`, `replayed_count` (`:3945-3951`). | EIE key `eie:sync:{t}:{entity_id}:{sha256(row)[:16]}` (`packet_router.py:370-374`); CEG node MERGE on `idproperty`; relationship idempotency on `(type, evidence_ref)` | `SyncRowRejectedError` (whole batch), `ObservedAssociationRejectedError`, `FeatureNotEnabled`, `ExecutionError` |
| `match` | graph (`hco:22`) | consumer (EIE sender `GraphSyncClient` is dead code, `orchestration_layer.py:96-103`) | `query`, `match_direction`, `top_n` (1..1000), `weights`; or `contract_version` MatchRequest (`:847-1003`) | `{status, candidates, query_id, match_direction, total_candidates, execution_time_ms, scoring_meta}` (`:1300-1313`) | none (read) | `ValidationError` (null gate param under `STRICT_NULL_GATES`, no candidate entity), `ExecutionError` |
| `outcomes` | graph (`hco:24`) | consumer | `match_id`, `candidate_id`, `outcome` ∈ {success, failure, partial}; `outcome_id`, `value` (`:3034-3057`) | `{status:"recorded", outcome_id[, replayed]}` | MERGE on `outcome_id` + uniqueness constraint (`OUTCOME_IDEMPOTENCY_ENABLED=True`) | `ValidationError` on a mismatched replay, `OutcomeVocabularyUndeclaredError` |
| `resolve` | graph (`hco:30`) | consumer | `entity_type`; `mode:"batch"` or `source_id`/`target_id`/`confidence`/`signal` | `{status:"resolved"\|"batch_resolved", …}` | **none (CREATE)** | `ValidationError` |

### 1b. OPERATOR_ONLY (`action_ownership.py:57-65`)

| Action | Owner | Worker rule | Notes |
|---|---|---|---|
| `health-assess` | graph (`hco:37`) | only `enrichment-engine` may originate as a worker: `WORKER_ORIGINATED_OPERATOR_ACTIONS` (`ao:97-101`) | `subaction` ∈ {health_assess, health_batch_assess, health_report} (CEG `:3466-3487`); EIE sends it from the sweep (`enrichment_sweep.py:55`) |
| `score` | graph (`hco:42`) | **every worker refused**, EIE included (`ao:91-96`) | CEG refuses unless `REMOTE_SCORE_ENABLED` and `EVIDENCE_IDENTITY_PER_TYPE_ENABLED` are on and the caller holds an explicit `score:read` grant (`:3706-3771`) |

### 1c. CONSUMER (`action_ownership.py:79-82`)

`CONSUMER_ACTIONS` is a class label, not a restriction (`ao:67-75`).

| Action | Owner | Notes |
|---|---|---|
| `enrichment-sweep` | enrichment-engine (`hco:59`) | advertised only when `L9_ENRICHMENT_SWEEP_ENABLED` is on (EIE `app/main.py:244-246`) |
| `enrichment-sweep-results` | enrichment-engine (`hco:60`) | reads `sweep_id`, `cursor` |

### 1d. Frozen negatives (verified at the heads)

- CEG advertises exactly `match, sync, outcomes, resolve, health-assess, score`. It never advertises `enrich`, `admin`, `health` or `healthcheck` (`engine/spec.yaml:36-57`, guarded by `tests/architecture/test_seam_gate_only.py`).
- EIE advertises `converge, graph-inference-result, enrich, enrich-and-sync`, plus the two sweep actions behind their flag (`app/main.py:225-246`).
- No `score-invalidate` sender exists (`packet_router.py:15-18`). EIE's converge overlay still grants it to `eie-e2e` (`tests/e2e/compose.converge.yml`). That grant is stale: owner EIE-W, disposed by not granting it in this lane's rail.
- Gate's `canonical_actions_only` defaults to on and cannot be turned off in staging or prod (`settings.py:190, 297-305`). It refuses to register or route an unknown action (`node_registry.py:133-136, 208-214`), which returns 404 `not_found`.

### 1e. Gate error taxonomy (frozen observation; `G/api/errors.py`)

| Status | Code | Conditions |
|---|---|---|
| 400 | `invalid_transport_packet` | bad, unsigned or unknown-key signature; wrong destination; skew or TTL failure |
| 403 | `action_not_permitted` | no policy, wrong source, **wrong tenant**, action not in policy, kind violation |
| 403 | `routing_policy_violation` | — |
| 404 | `not_found` | unknown action, or no registrant |
| 503 | `no_healthy_node` | — |
| 429 | `admission_rejected` | — |
| 504 | `execution_timeout` | — |
| 502 | `worker_response_untrusted` | — |
| 502 | `worker_transport_failed` | — |
| 400 | `invalid_request` | replay of a `packet_id` within 300 s |
| 401 | `admin_auth_failed` | — |

Idempotency cache:
- Key: `tenant\x1faction\x1fkey`. The store is process-local, `DURABLE=False`.
- TTL is `GATE_IDEMPOTENCY_TTL_SECONDS` (86400).
- Only terminal success is cached.
- `GATE_REPLAY_SAFE_ACTIONS = ∅`, so every action gets exactly 1 attempt (`G/resilience/replay_safety.py:28-92`).

### 1f. Signed dispatch lineage (frozen; `G/routing/dispatch.py:90-142`, SDK `bc1c74c` `transport/packet.py:110-148`)

**Child packet**
- Addressing: `source_node=gate`, `reply_to=gate`, `destination_node=<owner>`.
- Provenance: `origin_kind=gate`, `resolved_by_gate=True`, `route_kind=external_ingress`, `original_source_node=<caller>`.
- Lineage: new `packet_id`, `causation_id=parent`, `parent_id=parent`, same `root_id`, `generation+1`.
- `idempotency_key`, `trace_id` and `correlation_id` are inherited.
- `hop_trace` is reset to the Gate hops.
- Gate signs the child with its own key.

**Worker response (SDK `gate_authority/dispatch.py:428-474`)**

It must have:
- `source==target` and `destination==gate`;
- the same action, `root_id`, tenant, `correlation_id` and `idempotency_key`;
- `causation_id == dispatched packet_id`.

A response that fails these checks is answered with 502.

**Returned to the caller (`G/api/dependencies.py:163-183`)**

The worker's response is returned **re-signed with Gate's key**. It is not re-derived, so `source_node` stays the worker's node and `causation_id` points at Gate's dispatch packet.

## 2. Typed conversion matrix — EIE cartridge vs CEG pack

The conversion matrix was produced by running the owners' code, not by reading it. These are local-verified receipts at the pinned images, not real-process evidence.

| Probe | Receipt | sha256 |
|---|---|---|
| EIE `check_compatibility` (rule set `eie-ceg-sync/v2`) + `project_graph_sync_row`, in the EIE image | `receipts/a1/eie_projection.json` | `36019fb1…06c2c206` |
| CEG `SyncRowContract.defects`, in the CEG image | `receipts/a1/ceg_row_contract.json` | `1adaa5fc…021606cb` |

Both probes ran over `tests/e2e/gate_loop/vectors/sync_rows.json`. Probes: `tests/e2e/gate_loop/probes/`. Compatibility verdict: **pass** for `plasticos` (EIE 0.6.2 vs TEST_ONLY CEG 1.6.0) and for `synthneutral` (0.1.0 vs 0.1.0), with zero defects.

### 2a. Endpoint `/v1/sync/facilities` (plasticos)

| Aspect | EIE (`domains/plasticos/spec.yaml:168-179`) | CEG (pack `sync`) | Status |
|---|---|---|---|
| `entity_type` | `facilities` (last path segment) | same | ALIGNED |
| `targetnode` | Facility | Facility | ALIGNED |
| `idproperty` | facility_id (string, required) | facility_id (string, required) | ALIGNED |
| `batchstrategy` | UNWINDMERGE | UNWINDMERGE | ALIGNED |
| `childsync` | field processed_polymers → PolymerFamily by `code`, PROCESSES, parenttochild | same, + `edgeidentity: line_id`, `edgeproperties [line_id, direction, form]`, `fromrow [direction, form]` | ALIGNED. The extra keys are CEG write semantics; EIE sends the element fields they read. |
| `targetedge` / relationship sync | none | none for plasticos | NOT_APPLICABLE (no relationship endpoint) |
| observed association | none (`observed_association_send_enabled=False`) | `OBSERVED_ASSOCIATION_REQUIRED=False`, no `observedfrom` | NOT_APPLICABLE for plasticos |
| evidence namespaces | no `refnamespace` | no `refnamespace` | NOT_APPLICABLE |

### 2b. Facility row properties (EIE sends ⊆ CEG declares)

| Property | EIE type | CEG type | Native wire | Status |
|---|---|---|---|---|
| facility_id | string, required | string, required | str | ALIGNED |
| name | string, required | string, required | str | ALIGNED |
| min_density, max_density | float | float | int\|float, not bool, finite | ALIGNED |
| min_mfi, max_mfi | float | float | same | ALIGNED |
| contamination_tolerance | float (percent 0-100) | float (percent 0-100) | same | ALIGNED |
| capacity_tons_month | float (t/month) | float | same | ALIGNED |
| tier | string | string | str | ALIGNED |
| processed_polymers | list<map{code string required, line_id, direction, form}> | identical | JSON array of objects, **never a JSON string** | ALIGNED (vector PX-NEG-01 refused by both engines) |
| lat, lon, credit_score, community_id, rate_per_ton, rate_confidence, last_transaction_date, entity_ref, food_grade_certified | not declared (`ceg_owned`) | declared | — | CEG-OWNED. EIE never sends them: PX-NEG-05 `credit_score` is dropped by EIE projection. **FINDING F-A1-03**: CEG's row contract accepts the same row from any sync caller, so property ownership is not enforced at CEG ingress. Owner: CEG-W. |
| evidence-only fields (city … application_notes) | `evidenceonly: true` | not declared | — | NEVER PROJECTED (EIE `spec_types.py:422-431`) |

### 2c. Vector outcomes (both engines agree on every vector)

| Vector | Frozen expectation | EIE projection | CEG row contract |
|---|---|---|---|
| PX-POS-01 (two lines, HDPE in / PP out) | accept | row, no defects | no defects |
| PX-POS-02 (tolerance unknown) | accept | row, no defects | no defects |
| PX-POS-03 (tolerance 1 %) | accept | row, no defects | no defects |
| PX-NEG-01 (stringified map) | refuse | refused: not a list | refused: expected list<map> |
| PX-NEG-02 (`min_density:"0.94"`) | refuse | refused | refused |
| PX-NEG-03 (child missing `code`) | refuse | refused | refused |
| PX-NEG-04 (undeclared `bogus_field`) | drop or refuse | dropped | accepted with the default `SYNC_UNDECLARED_PROPERTY_REFUSAL_ENABLED=False`; refused when it is on |
| PX-NEG-05 (CEG-owned `credit_score`) | EIE never sends it | dropped | accepted (F-A1-03) |
| SN-POS-01 | accept | row | no defects |
| SN-NEG-01 (`max_load:"heavy"`) | refuse | refused | refused |

## 3. Frozen PlasticOS witness behaviour (CEG pack `gates`)

**Polymer hard gate, direction `supply_opportunity_to_buyer_facility`: `polymer_capability_gate`**
- Gate type is traversal: `(Facility)-[:PROCESSES]->(PolymerFamily{code:$polymer_type})`, with edge predicates `direction == "in"` and `form` present. `nullbehavior: fail`.
- So a facility qualifies only through an *in* line with a form. Witnesses:
  - PM-POS-01 expects tp-fac-001 (HDPE in, Regrind) and tp-fac-002.
  - PM-NEG-02 expects no candidate for PP, because tp-fac-001's PP line is `out`.

**Polymer hard gate, direction `buyer_demand_to_supply_opportunity`: `fulfills_polymer_gate`**
- `(SupplyOpportunity)-[:FULFILLS_POLYMER]->(PolymerFamily)` together with `active_supply_available` (fail-on-null).
- Facility capability is never treated as active supply. Witness PM-NEG-03 expects no candidate with no SupplyOpportunity.

**Unknown tolerance stays eligible**
- `contamination_threshold` (`contamination_tolerance >= contamination_pct`, `nullbehavior: pass`).
- A facility with no tolerance passes (tp-fac-002). A known, unacceptable tolerance fails (tp-fac-003 at 1 % against a 3 % load).
- Under `STRICT_NULL_GATES=True`, a **query** parameter that is null raises instead of passing (CEG `handlers.py:974-985`). Witnesses therefore always supply `contamination_pct` when they test that gate.

**Observed vs inferred**
- Synced Facility properties are observed evidence (EIE field provenance).
- A CEG inference reaches EIE only as `graph-inference-result` outputs, carrying `rule`, `confidence` and `provenance`.
- `_apply_graph_targets` never overrides an entity-supplied value. It only fills unknowns or replaces lower-confidence loop output (EIE `convergence_controller.py:677-707`).

**Relationship identity**
- `(tenant, relationship_type, evidence_ref)`: CEG idempotency on `(type, evidence_ref)` within the tenant database (`handlers.py:3850-3919`, which needs Neo4j Enterprise 5.7+).
- PROCESSES capability lines are keyed by `(tenant, facility_id, line_id)` through `edgeidentity: line_id`. A line with no `line_id` keeps an unkeyed polymer link and is not a buyer line.

## 4. GateClient factory and registration model (confirmed)

**EIE**
- Client: `build_gate_client` builds `GateClient(build_gate_client_config(...))` (`app/services/gate_client.py:28-134`). `local_node="enrichment-engine"` is forced, and the SDK env factory supplies everything else.
- Signing: `L9_SIGNING_KEY`/`_KEY_ID`/`_ALGORITHM`, `L9_VERIFYING_KEYS_JSON` and `L9_REQUIRE_SIGNATURE`.
- Registration: `register_node(...)` posts to `POST {gate}/v1/admin/register` with `X-Admin-Token`, once at startup and then every 300 s (`app/main.py:264-342`). The health path it registers is `/api/v1/health`.

**CEG**
- Client: `get_gate_client()` builds `GateClient(get_gate_client_config_from_env())` (`engine/gate_client.py`).
- Registration: SDK participation reads `GATE_NODE_SPEC_PATH=engine/spec.yaml`. The health path is `/v1/health`.

**SDK `bc1c74c` (`gate/config.py`)**
- `GateClient` takes the ed25519 private key from **`L9_SIGNING_KEY`**.
- The worker runtime config refuses `L9_SIGNING_KEY` when the algorithm is ed25519, and wants `L9_SIGNING_PRIVATE_KEY` (`runtime/config.py:200-204`).
- **FINDING F-A1-04** (SDK owner): a node that is both a worker and an egress client cannot configure ed25519 through env alone. A7 probes this at runtime.

**Registration hijack surface (`G/services/admin_registration_service.py`, `G/routing/action_ownership.py:152-204`)**
- A registration requires the admin token and a canonical-owner match. The owner is self-asserted in `metadata.owner`, or taken from the node-name alias.
- A registration is **not** bound to the node's signing key.
- The token comparison uses `!=` and is not constant-time (`GET /v1/registry` uses `compare_digest`).
- **FINDING F-A1-05** (Gate owner): an admin-token holder can repoint `graph` or `enrichment-engine` to any `internal_url`. That mis-route is then refused only because the response signature and answers-dispatch checks fail (502). A7 witnesses it.

No new field, action, payload key or caller policy is needed to freeze sections 1–3. The CTO-QUESTIONs below are the points where the frozen seam statement and the code disagree.

## 5. Blocked / gap register (owner, evidence)

| ID | Gap | Owner | Evidence | Affects |
|---|---|---|---|---|
| F-A1-01 | The node `sync` ack carries no `created`, `replayed` or `requested` counts, so GRAPH_ACKNOWLEDGED (`created+replayed==requested`) is unprovable for node endpoints. Only `synced_count==len(batch)` exists. | CEG-W (ack); CTO (the definition is a payload-key question) | CEG `handlers.py:1605-1625`; EIE check `packet_router.py:259-288` | P07, P16, P19 |
| F-A1-02 | CEG inference is reachable only through operator `admin` / `emit_inference_feedback`, which Gate never routes. It infers from the caller-supplied `entity`, not from a graph read. The plasticos design pack authorizes no rule. | CEG-W (A3/A6) | CEG `handlers.py:2499-2571`; pack has no `plugins` | P22, P23, P24, P30 |
| F-A1-03 | CEG accepts CEG-owned properties on `sync` from any authorized caller. | CEG-W | vector PX-NEG-05 | P04 (disposed: EIE never sends them) |
| F-A1-04 | SDK ed25519 env layout differs between `GateClient` and the worker runtime. | SDK owner | SDK `bc1c74c` `gate/config.py` vs `runtime/config.py:200-204` | P07, P08, P12 |
| F-A1-05 | A registration is not bound to the node key; the admin-token comparison is not constant-time. | Gate owner | `admin_registration_service.py:64-68` | P10, P11 — **DEFERRED** by the operator (2026-11-24); not counted against A8 |
| F-A1-06 | `_graph_pending` and the return-channel queue are in memory and lost on restart. Nothing drains them at shutdown. | EIE-W (A2/A4) | `side_effect_coordinator.py:176-244`; `graph_return_channel.py:99-167` | P14, P15, P16, P21, P25 |
| F-A1-07 | `graph-inference-result` intake does no dedup, so a redelivered packet enqueues duplicates. | EIE-W (A5) | `graph_return_channel.py:55-96` | P23, P28 |
| F-A1-08 | The `infer_geography_from_postal_code` output field `region` is not declared on the EIE plasticos cartridge, and `zip_code` is evidence-only in EIE and absent from CEG's Facility. A plasticos inference therefore cannot become a changed EIE fact unless the cartridge declares it, which is a spec change. | CTO (spec freeze) | EIE cartridge `subjects`; CEG pack ontology | P22, P27, P30 |
| F-A1-09 | EIE's router reader is built without `require_published`, while the startup reader is built with it outside local/dev/test. | EIE-W | `packet_router.py:1240-1246` vs `app/main.py:112-115` | P01 (staging) |
| F-A1-10 | The stale `score-invalidate` grant in EIE's converge overlay. | EIE-W | `tests/e2e/compose.converge.yml` | P06 |
| F-A1-11 | Gate's Docker rail sets no caller policy in staging, though staging requires one for every verifying key (`settings.py:412-422`). It also signs the driver with **Gate's own key**, so it cannot exercise consumer caller policy. | Gate owner (rail) | Gate `tests/e2e/docker/compose.yml`, `run_e2e.sh` drive() | P02, P10, P11 |
| F-A1-12 | EIE runs `L9_ENVIRONMENT=test` in the e2e rail, because the deterministic provider requires it. That forces SDK `dev_mode` on at EIE ingress: unsigned packets are accepted, and only GATE-KEY-01 key-id binding refuses non-Gate packets. | EIE-W (declared deviation) | `tests/e2e/compose.converge.yml` comments; `app/main.py:382-414` | P07, P11 |

## 6. Fixture vectors

| File | Contents |
|---|---|
| `tests/e2e/gate_loop/vectors/sync_rows.json` | 10 sync-row vectors and 5 match vectors, across both TEST_ONLY tenants |
| `tests/e2e/gate_loop/cartridges/synthneutral/spec.yaml` | EIE synthetic cartridge |
| `Cognitive.Engine.Graphs tests/fixtures/test_only_packs/` | CEG packs and the `TEST_ONLY.json` manifest |

## 7. Amendments after A1 (r2) and the A8 baseline at main

- **TEST_ONLY packs r2.** Only `capabilities` changed; the sync, ontology and gates sections are byte-identical to r1. The A1 conversion matrix is unchanged because `capabilities` takes no part in it.
- **A7 trust and policy.** See `A7_TRUST_POLICY_MATRIX.md`. New findings:
  - F-A7-01: Gate private-key self-resolution.
  - F-A7-02: the same defect in the SDK client.
  - F-A7-03: algorithm-confusion forgery (CTO-BLOCKER).
- **A8 baseline at main.** Ledger: `acceptance_ledger.csv`. Receipts: `receipts/a8_baseline_main/`. Rail: `tests/e2e/gate_loop/`.

| Verdict | Cases |
|---|---|
| PASS (real-process, ed25519) | P03 (topology), P09, P10, P13, P17\*, P19\*, P33\*, P34\* |
| BLOCKED | P07, P18, P05 (EIE ed25519 egress, F-A1-04); P11 (F-A7-03); P08 (CEG egress, F-A1-04); P22 (F-A1-02) |
| FAIL (gap evidence) | P14 (F-A1-06: no durable graph intent, no outbox table in EIE Postgres); P23 (F-A1-07: an identical inference delivery is queued twice) |
| NOT_RUN | P12 (signer half PASS; lock-mismatch half not exercisable because all images carry identical SDK code); every other case |
| NOT_APPLICABLE | P36 |

\* CEG-side fixture rows travel through Gate signed with the `eie-gl` key held by the driver (rig), because EIE's own egress is blocked at main. These cases assert CEG ingress, write and match behaviour, not EIE egress.

These verdicts are main-baseline only. The CTO re-runs the same rail at the integrated heads for the case verdicts that count (P39).

## 8. CTO dispositions (frozen with A1, 2026-11-22)

| Finding | Disposition | How the rail applies it |
|---|---|---|
| F-A1-01 | Node GRAPH_ACKNOWLEDGED means: right tenant and `entity_type`, `synced_count == len(batch)`, and `rejected_count` absent or 0. `created + replayed == requested` applies to relationship endpoints only. | `rail.py` `node_acked()`, asserted in P17 and P19 |
| F-A1-04, F-A1-05, F-A1-11 | BLOCKED_WITH_EXACT_OWNER: Gate_SDK, Constellation.Gate | each witnessed with a receipt (`A7_TRUST_POLICY_MATRIX.md` §6) |
| F-A1-05 (2026-11-24) | DEFERRED by the operator (owner Constellation.Gate) | witness kept (`receipts/a8_integrated_v134/F-A1-05.ed25519.json`); the defenses held: owner collision 400, misrouted packet refused 403/502; not counted against the A8 verdict |
| F-A1-08 | No spec change. The changed-fact witness runs on `synthneutral`, whose TEST_ONLY pack r3 and cartridge declare `postal_code` and `region` and authorize `infer_geography_from_postal_code`. Plasticos is the witness for the justified no-op. | `rail.py` `c_feedback`: P27, P30, P22 |
| F-A1-10 | Fixed in this lane: `tests/e2e/compose.converge.yml`, one line. | — |
| F-A1-12 | CTO-BLOCKER (EIE-W): the deterministic provider is coupled to dev_mode. | — |
| F-A1-03, F-A1-06, F-A1-07, F-A1-09 | Coding lanes; left open in the ledger. | — |

TEST_ONLY packs r3:

| Pack | sha256 |
|---|---|
| plasticos | `e6ec5b013c35ad7c6053a26f79bb7734fd110186247c162fe944fc93a8034448` |
| synthneutral | `72aa88c8c237dfa2b61d754aa237e7a3051191668ef3ae93206fe9e52701d519` |
| EIE synthneutral cartridge | `65f68382b5c5143dfe36683daac0f1a582b86f514646d62d1c721e0f59ff805b` |

C-18 L9_META headers are at the top of each pack. The r3 compatibility probe passes for both tenants (`receipts/a1/eie_projection_r3.json`).
