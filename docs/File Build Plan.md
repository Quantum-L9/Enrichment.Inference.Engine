# ENRICH Engine — Convergence loop file plan (historical + status)

**LAST_REVIEWED:** 2026-04-11

This document was the original build plan for the enrichment ↔ inference ↔ convergence loop. **As of the review date, all twelve paths below exist in the tree**; remaining work is integration hardening, product behavior, and tests—not greenfield file creation.

The live HTTP surface for convergence is under the **`/api/v1/...`** router (see `app/main.py` and `app/api/v1/converge.py`), not bare `/v1/converge`.

***

## Build order (original) — implementation status

| # | Component | Path | Status (2026-04) |
|---|-----------|------|------------------|
| 1 | Field confidence | `app/models/field_confidence.py` | Present |
| 2 | Loop schemas | `app/models/loop_schemas.py` | Present |
| 3 | Rule loader | `app/engines/inference/rule_loader.py` | Present |
| 4 | Rule engine | `app/engines/inference/rule_engine.py` | Present |
| 5 | Grade engine | `app/engines/inference/grade_engine.py` | Present |
| 6 | Cost tracker | `app/engines/convergence/cost_tracker.py` | Present |
| 7 | Pass telemetry | `app/engines/convergence/pass_telemetry.py` | Present |
| 8 | Loop state store | `app/engines/convergence/loop_state.py` | Present |
| 9 | CRM field scanner | `app/services/crm_field_scanner.py` | Present |
| 10 | Enrichment profiles | `app/services/enrichment_profile.py` | Present |
| 11 | Schema proposer | `app/engines/convergence/schema_proposer.py` | Present |
| 12 | Convergence API | `app/api/v1/converge.py` | Present |

**Next focus:** behavioral tests, Tier-2 enforcement (`tests/contracts/tier2/`), and contract alignment with [`docs/contracts/`](docs/contracts/) — not adding these files from scratch.

***

## File 1 — `app/models/field_confidence.py`

**Why first:** Every downstream file needs per-field confidence. The existing consensus engine returns a single float for the entire entity. SCORE, HEALTH, the uncertainty engine, and the convergence check all need to know *which fields* are weak, not just that the entity as a whole scored 0.82.[^2][^1]

**What it does:**

- `FieldConfidence` — Pydantic model: `field_name`, `value`, `confidence` (0.0–1.0), `source` (enum: `crm`, `enrichment`, `inference`), `variation_agreement` (e.g., 4/5 = 0.80), `pass_discovered` (int), `kb_fragment_ids` (list)
- `FieldConfidenceMap` — dict wrapper keyed by field_name with aggregate methods: `weakest_fields(n)`, `fields_below(threshold)`, `avg_confidence`, `coverage_ratio`
- `compute_field_confidence(consensus_payloads, target_schema)` — takes the raw validated payloads from consensus and computes per-field agreement. If 4/5 variations return `materials_handled: ["HDPE", "LDPE"]` with same values, that field gets 0.80 confidence. If only 2/5 agree on `contamination_tolerance: 0.03`, that field gets 0.40[^1]
- Serializable to dict for TransportPacket / payload embedding

**Feeds into:** loop_schemas (#2), pass_telemetry (#7), enrichment_profiles (#10), schema_proposer (#11), and eventually HEALTH + SCORE services.

***

## File 2 — `app/models/loop_schemas.py`

**Why second:** The convergence loop introduces request/response shapes that don't exist in `schemas.py`. Every convergence file needs these types.[^1]

**What it does:**

- `ConvergeRequest` — extends EnrichRequest with: `domain` (str), `max_passes` (int, default 5), `max_budget_tokens` (int, default 50000), `approval_mode` (enum: `auto` | `human`), `convergence_threshold` (float, default 2.0)
- `PassResult` — per-pass snapshot: `pass_number`, `mode` (discovery/targeted/verification), `fields_enriched` (list[str]), `fields_inferred` (list[str]), `field_confidences` (FieldConfidenceMap), `uncertainty_before`, `uncertainty_after`, `tokens_used`, `duration_ms`
- `ConvergeResponse` — full loop result: `entity_id`, `passes` (list[PassResult]), `final_fields` (dict), `final_field_confidences` (FieldConfidenceMap), `schema_proposals` (list[SchemaProposal]), `convergence_reason` (enum: `threshold_met` | `budget_exhausted` | `max_passes` | `human_hold`), `total_tokens`, `total_cost_usd`, `domain_yaml_version_before`, `domain_yaml_version_after`
- `SchemaProposal` — proposed field additions: `field_name`, `field_type`, `source` (enrichment/inference), `fill_rate`, `avg_confidence`, `sample_values`, `proposed_gate` (optional), `proposed_scoring_dimension` (optional)

***

## File 3 — `app/engines/inference/rule_loader.py`

**Why third:** The inference engine needs structured rules loaded from YAML before it can fire anything. `inference_bridge.py` currently has hardcoded plastics rules — this replaces that with a domain-agnostic loader.[^1]

**What it does:**

- `RuleDefinition` — Pydantic model: `rule_id`, `conditions` (list of `{field, operator, value}`), `outputs` (list of `{field, value_expr, derivation_type}`), `confidence` (float), `priority` (int), `domain` (str), `description` (str)
- Supported operators: `CONTAINS`, `EQUALS`, `GT`, `LT`, `GTE`, `LTE`, `IN`, `NOT_IN`, `IS_TRUE`, `IS_FALSE`, `EXISTS`
- `RuleRegistry` — indexed by trigger fields for O(1) candidate lookup. When the engine asks "which rules might fire given I just filled `materials_handled`?", the registry returns only rules where `materials_handled` appears in conditions[^1]
- `load_rules(domain_yaml_path)` — parses the `inference_rules` section of a domain YAML, validates syntax at load time (bad operator → error, not silent), builds registry
- `reload()` — hot-reload support: re-reads YAML, rebuilds registry, swaps atomically (no request sees a half-loaded state)

**Example rule from `plastics_recycling.yaml`:**

```yaml
inference_rules:
  - rule_id: grade-assignment-hdpe
    conditions:
      - field: materials_handled
        operator: CONTAINS
        value: HDPE
      - field: contamination_tolerance_pct
        operator: LT
        value: 0.05
    outputs:
      - field: material_grade
        value_expr: "B"
        derivation_type: classification
    confidence: 0.90
    priority: 10
```

***

## File 4 — `app/engines/inference/rule_engine.py`

**Why fourth:** This is the standalone deterministic inference engine that replaces the non-existent `plasticos.inference.engine` Odoo model. It fires rules against enriched feature vectors and produces derived fields.[^2]

**What it does:**

- `infer(entity_fields: dict, registry: RuleRegistry) → InferenceResult`
- For each rule in the registry where all trigger fields exist in the entity:
  1. Evaluate conditions (all must pass — AND logic within a rule)
  2. If rule fires → add outputs to derived_fields with full derivation chain
  3. Track `rules_fired`, `rules_evaluated`, `rules_skipped` (missing trigger fields)
- `InferenceResult` — `derived_fields` (dict), `rules_fired` (list[RuleFired]), `derivation_chains` (dict mapping field → list of rule_ids that produced it), `inference_confidence` (min confidence across all fired rules)
- Priority handling: if two rules produce the same output field, highest priority wins. Tie → highest confidence wins
- **Cascading inference:** after first pass, check if newly derived fields trigger additional rules. Loop until no new rules fire (max 3 cascade levels to prevent infinite loops)[^1]
- Domain-agnostic: works with any RuleRegistry from any domain YAML

***

## File 5 — `app/engines/inference/grade_engine.py`

**Why fifth:** Grade/tier classification is the highest-value inference output. `material_grade`, `facility_tier`, `application_class`, `buyer_class` — these are what make entities matchable in the graph. The rule engine (#4) handles generic IF/THEN rules; the grade engine handles the specific pattern of matching a feature vector against grade envelopes.[^1]

**What it does:**

- `GradeDefinition` — from KB YAML: `grade_id`, `grade_label` (A/B+/B/C/D), `conditions` (dict of field → range/value), `tier` (str), `application_class` (str)
- `classify(entity_fields: dict, grade_defs: list[GradeDefinition]) → GradeResult`
- Scoring: for each grade definition, compute match_score = (number of conditions met / total conditions). Best-fit grade = highest match_score above threshold (0.6 default)[^1]
- `GradeResult` — `grade`, `tier`, `application_class`, `quality_tier`, `match_score`, `conditions_met`, `conditions_missed`, `fallback_used` (bool)
- SIMILAR_TO fallback: if no grade meets threshold, return closest grade with `fallback_used: true` and reduced confidence
- Loads grade definitions from the domain KB (e.g., `plastics-recycling-v8.yaml` `grades` section)

***

## File 6 — `app/engines/convergence/cost_tracker.py`

**Why sixth:** The convergence loop can burn unlimited Sonar tokens if convergence is slow. This enforces a budget ceiling and tracks cost per pass.[^1]

**What it does:**

- `CostTracker` — initialized with `max_budget_tokens` (from ConvergeRequest)
- `record_pass(pass_number, tokens_used)` — accumulates spend, computes cost_usd (tokens × rate)
- `budget_remaining() → int` — tokens left before hard stop
- `can_continue() → bool` — returns False if budget exhausted
- `cost_per_field(total_fields_discovered)` — unit economics: total_cost / fields_discovered
- `to_summary() → CostSummary` — `total_tokens`, `total_cost_usd`, `tokens_per_pass` (list), `cost_per_field`, `budget_utilization_pct`
- Token rate configurable in Settings (default: $0.005/1K tokens for sonar-reasoning)
- Integrated into `convergence_controller.py`'s main loop: checked between passes

***

## File 7 — `app/engines/convergence/pass_telemetry.py`

**Why seventh:** The core product claim — "each pass gets better" — is currently an assertion, not a measurement. This makes it provable.[^1]

**What it does:**

- `PassTelemetryCollector` — accumulates PassResult objects across the loop
- `record_pass(pass_result: PassResult)` — stores snapshot
- `confidence_delta(pass_a, pass_b)` — per-field and aggregate confidence improvement
- `uncertainty_delta(pass_a, pass_b)` — uncertainty reduction between passes
- `diminishing_returns_check(window=2)` — if the last N passes improved uncertainty by less than 5%, signal convergence even if threshold not technically met
- `convergence_report() → ConvergenceReport` — full pass-over-pass comparison: fields discovered per pass, confidence trajectory, uncertainty trajectory, tokens spent per pass, ROI per pass (fields_gained / tokens_spent)
- This is the data that proves the upsell: "Pass 1 cost $0.25 and found 12 fields. Pass 2 cost $0.08 and found 6 more. Pass 3 cost $0.03 and confirmed 3. Total: $0.36 for 21 fields at 0.91 confidence."[^1]

***

## File 8 — `app/engines/convergence/loop_state.py`

**Why eighth:** `convergence_controller.py` runs in-memory. A crash mid-loop loses all multi-pass progress. For batch convergence of 5,000 entities, this is unacceptable.[^1]

**What it does:**

- `LoopState` — Pydantic model: `run_id` (UUID), `entity_id`, `domain`, `state` (enum: `running`, `converged`, `budget_exhausted`, `max_passes`, `human_hold`, `failed`), `current_pass`, `passes_completed` (list[PassResult]), `accumulated_fields`, `accumulated_confidences` (FieldConfidenceMap), `cost_summary` (CostSummary), `created_at`, `updated_at`
- `LoopStateStore` — abstract interface with two implementations:
  - `RedisLoopStateStore` — serializes LoopState to Redis with TTL (24h default). Fast, ephemeral
  - `PostgresLoopStateStore` — persists to `convergence_runs` table. Durable, queryable
- `save(state)`, `load(run_id) → LoopState | None`, `resume(run_id)` — picks up from last completed pass
- `list_active(domain=None)` — returns all in-progress loops (for admin dashboard)
- `convergence_controller.py` calls `save()` after every completed pass. On restart, `resume()` loads state and continues from the last checkpoint

***

## File 9 — `app/services/crm_field_scanner.py`

**Why ninth:** This is the Seed tier trojan horse. Day 0: customer connects CRM. The scanner reads their existing field schema and generates the discovery report that converts free → $500/mo.[^1]

**What it does:**

- `scan_crm_fields(fields: list[CRMField]) → ScanResult` — accepts raw CRM field list (name + type pairs from Salesforce/HubSpot/Odoo API metadata)
- `CRMField` — `name` (str), `type` (str: string/float/bool/enum/list), `sample_values` (list, optional), `fill_rate` (float, optional)
- Maps CRM fields against the domain YAML's `ontology.nodes.properties` to identify: **matched** (CRM field maps to a known property), **unmapped** (CRM field exists but isn't in the domain), **missing** (domain property has no CRM field)[^1]
- `generate_seed_yaml(scan_result, domain_template) → str` — produces a `v0.1.0-seed` domain YAML containing only the customer's current fields
- `generate_discovery_report(scan_result) → DiscoveryReport` — the sales document: "You have 5 fields. Your domain needs 23. Here are the 18 you're missing, ranked by impact on matching quality."
- Impact ranking: fields that are gate-critical in the domain YAML rank highest (e.g., `contamination_tolerance` is a gate → missing it blocks all matching). Scoring-dimension fields rank second. Nice-to-have fields rank last

***

## File 10 — `app/services/enrichment_profile.py`

**Why tenth:** Nightly batch enrichment has no selection criteria. Running all 5,000 entities every night wastes tokens. The profile selects the highest-impact entities.[^1]

**What it does:**

- `EnrichmentProfile` — Pydantic model: `profile_name`, `selection_criteria` (`max_staleness_days`, `min_null_count`, `confidence_below`, `min_failed_matches`, `is_gate_critical_incomplete`), `batch_size`, `max_budget_tokens`, `schedule_cron`, `convergence_mode` (bool — run full loop or single pass)
- `select_entities(profile, entity_store) → list[EntityRef]` — queries the entity store, applies selection criteria, ranks by priority score:
  - Priority = `(null_count × 0.4) + (staleness_days × 0.3) + ((1 - confidence) × 0.2) + (failed_matches × 0.1)`
  - Respects `batch_size` ceiling
- `allocate_budget(entities, max_budget_tokens) → list[EntityBudget]` — distributes tokens across selected entities. High-uncertainty entities get more tokens (more variations). Low-uncertainty entities get fewer (verification only)[^1]
- `ProfileRegistry` — loads profiles from config. Default profiles: `nightly_stale` (entities not enriched in 30+ days), `high_null` (entities with >50% NULL fields), `failed_match` (entities involved in rejected matches), `new_intake` (entities synced in last 24h)

***

## File 11 — `app/engines/convergence/schema_proposer.py`

**Why eleventh:** This is the Discover tier ($2K/mo) unlock. After running the convergence loop on a batch, the system aggregates what it discovered and proposes schema changes to the customer.[^1]

**What it does:**

- `propose(batch_results: list[ConvergeResponse], current_yaml: DomainSpec) → SchemaProposalSet`
- Aggregates discovered fields across all entities in the batch. For each candidate field:
  - `fill_rate` — what % of entities have this field after convergence
  - `avg_confidence` — mean confidence across entities
  - `value_distribution` — histogram of observed values (for enums: top 5 values; for floats: min/max/mean/stddev)
  - `source` — `enrichment` (discovered from research) or `inference` (derived from rules)
- Filter: only propose fields with fill_rate > 0.60 and avg_confidence > 0.70[^1]
- `propose_gates(proposed_fields, domain_spec)` — if a proposed field is numeric with meaningful variance, propose it as a scoring dimension. If it's categorical with clear partitioning, propose it as a gate
- `SchemaProposalSet` — `proposed_fields` (list), `proposed_gates` (list), `proposed_scoring_dimensions` (list), `yaml_diff` (the exact YAML additions), `version_bump` (e.g., `0.2.0-discovered`)
- `apply(proposal, approval_decisions) → DomainSpec` — takes human approvals (Discover tier) or auto-approves (Autonomous tier), writes updated YAML with version bump

***

## File 12 — `app/api/v1/converge.py`

**Why last:** This wires everything above into HTTP endpoints. It's the thin API layer on top of the convergence controller.[^1]

**What it does:**

- `POST /api/v1/converge` — single-entity convergence (see OpenAPI / `converge.py`).
- `POST /api/v1/converge/batch` — batch convergence.
- `GET /api/v1/converge/{run_id}` — run status.
- `POST /api/v1/converge/{run_id}/approve` — approval step for gated flows.
- `GET /api/v1/converge/proposals/{domain}` — pending proposals.
- `POST /api/v1/scan` — CRM field discovery / scan entry point.

***

## Dependency Graph

```
schemas.py (exists)
    └─→ [^1] field_confidence.py
            └─→ [^2] loop_schemas.py
            │       └─→ [^6] cost_tracker.py ──→ [^8] loop_state.py
            │       └─→ [^7] pass_telemetry.py
            └─→ [^10] enrichment_profile.py
            └─→ [^11] schema_proposer.py

domain YAML (exists)
    └─→ [^3] rule_loader.py
            └─→ [^4] rule_engine.py
                    └─→ [^5] grade_engine.py

domain_yaml_reader.py (exists)
    └─→ [^9] crm_field_scanner.py

ALL OF THE ABOVE
    └─→ [^12] converge.py (API layer)
```

***

## What This Unlocks

| Revenue Tier | Files Required | Monthly |
|-------------|----------------|---------|
| **Seed** (free → conversion) | #1, #9 (field_confidence + crm_field_scanner) | $0 (but 50%+ conversion to Enrich)[^1] |
| **Enrich** ($500/mo) | + #10 (enrichment_profiles for nightly batch) | $500 |
| **Discover** ($2K/mo) | + #2–8, #11 (full convergence loop + schema proposals) | $2,000[^1] |
| **Autonomous** ($5K–10K/mo) | + #12 with auto-approval + graph integration (already built) | $5,000–10,000[^1] |

After these 12 files, the enrichment-inference engine is a complete, closed-loop system. The graph engine is already done. The next phase would be HEALTH (first downstream service), then SCORE — both of which consume field_confidence (#1) and convergence data from these files.[^1]

---

## References (in-repo)

- [ARCHITECTURE.md](ARCHITECTURE.md) — topology and entrypoints
- [ROADMAP.CONTRACTS.md](ROADMAP.CONTRACTS.md) — contract enforcement roadmap
- [docs/contracts/config/env-contract.yaml](docs/contracts/config/env-contract.yaml) — environment SSOT
- [app/main.py](app/main.py) — mounted routers and public routes
- [app/api/v1/converge.py](app/api/v1/converge.py) — convergence HTTP API

*(Historical presigned external links were removed — they expired and should not live in-repo.)*
