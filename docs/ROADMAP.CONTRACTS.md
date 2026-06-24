# ROADMAP.CONTRACTS.md

**LAST_REVIEWED:** 2026-04-11

Strategic contract/governance roadmap. For **authoritative env names**, use [`docs/contracts/config/env-contract.yaml`](docs/contracts/config/env-contract.yaml). For **day-to-day agent law**, use [AGENTS.md](AGENTS.md).

Here’s the highest-leverage read of the whole project state.

## 1. Current state vs finished state gap

### Current state

You already have a serious multi-plane contract system for EIE spanning:
- environment/config contracts
- persistent data/state contracts
- graph/storage contracts
- REST/OpenAPI contracts
- event/AsyncAPI contracts
- MCP tool contracts
- TransportPacket / packet protocol contracts (`docs/contracts/`, SDK)
- dependency contracts
- generation templates
- a structural contract test suite with manifest, fixtures, drift checks, and gap-aware TODO tests.

Architecturally, EIE is already modeled as:
- a multi-pass belief accumulation engine
- a schema-learning engine with approval flow
- a packet-addressable constellation node
- an MCP-exposed agent capability surface
- a dependency-aware enrichment/writeback system.

### Finished state

The finished state is not “better docs.” It is a self-admitting, autonomy-governed node substrate where a node can be:
- generated from templates
- admitted by machine-readable constitution
- routed by policy-aware kernel logic
- trusted according to runtime attestation and enforcement history
- safely composed with sibling nodes
- upgraded without semantic drift
- reused as a blueprint for future constellation nodes.

### The gap

The main gap is:

You have a strong descriptive contract system, but only a partial governance and enforcement system.

More concretely, what is still missing is:
- a unified node constitution
- action-level authority semantics
- stronger provenance requirements
- dependency degradation policy
- event reliability classes
- behavior/authority/failure enforcement tests
- runtime attestation as a first-class contract surface.

---

## 2. Highest-leverage insights

### Insight 1: The repo is not really a repo standard. It is a node constitution system

Your contracts now span all the planes that define a constellation citizen: interface, state, protocol, events, dependencies, and templates. That means the right abstraction is no longer `docs/contracts/`; it is `machine-admissible node constitution.`

### Insight 2: The most valuable asset is not the API surface. It is the state transition model

The biggest structural advantage in EIE is the combination of:
- EnrichmentResult
- FieldConfidenceHistory
- ConvergenceRun
- SchemaProposalRecord

That means your node is already modeled as:
- iterative belief formation
- confidence trajectory
- resumable convergence
- governed schema evolution

This is much more reusable and autonomy-ready than ordinary service contracts.

### Insight 3: Templates are the bridge from one node to many

Wave 5 is strategically huge. The API, prompt, tool, data-model, and event templates mean you now have the seed of spawnable node generation.

Without templates, you have one well-documented node.
With templates plus constitution plus enforcement, you get a constellation factory.

### Insight 4: The core risk is not missing contracts. It is flattened authority

Today, enrich, discover, converge, and writeback are named, but not fully governed as different authority classes. writeback exists as a distinct tool/action, but it is not yet constitutionally separated enough from inferential actions.

That means your sharpest side-effect surface is still under-constrained.

### Insight 5: Structural tests are already strong enough to support a second enforcement layer

The test suite already proves:
- files exist
- schemas align
- packet invariants exist
- MCP registry drift is caught
- TODO gaps are tracked
- fixtures exist for packet/request/response examples.

That means you do not need a new testing philosophy.
You need a second test tier for:
- behavior
- authority
- failure
- replay
- degradation
- provenance

### Insight 6: Your current fixture set is under-leveraged

The existing fixtures are mostly happy-path witnesses. They are not yet a behavioral corpus.

Once fixtures are reorganized into valid/invalid/authority/failure/replay/event/provenance sets, they become reusable fuel for:
- enforcement tests
- future node templates
- cross-node replay
- onboarding and regression
- runtime simulation

### Insight 7: Dependency contracts are useful, but the real value comes when they become policy-bearing

Today dependencies are documented by role and requiredness, but not yet by:
- per-action criticality
- degradation behavior
- autonomy impact
- admission impact.

That is the missing link between infra and governance.

### Insight 8: Event contracts are currently the biggest hidden systemic weakness

Events are clearly documented but default to at_most_once, no retry, no DLQ, fire-and-forget.

That is acceptable for telemetry.
It is not sufficient for governance, coordination, or authoritative state changes.

### Insight 9: Provenance is present in fragments, but not yet elevated into a shared primitive

You already carry:
- kb_content_hash
- kb_fragment_ids
- inference_version
- correlation_id
- packet content_hash
- lineage metadata.

That is enough to bootstrap a shared provenance schema now. Once you do, it will amplify:
- replay
- trust scoring
- admission control
- drift analysis
- debugging

---

## 3. Why each matters

### Node constitution

This matters because it collapses many separate contract files into one machine-admissible truth object. That unlocks:
- automated admission
- routing eligibility
- template instantiation
- trust scoring
- runtime attestation checks

### State transition model

This matters because it is your strongest reusable primitive. Any future node that accumulates belief, confidence, proposals, or evidence can inherit this pattern instead of inventing a new one.

### Templates

This matters because templates are what convert architecture into replication. They are your path from EIE to a family of constellation nodes.

### Action authority

This matters because without it, autonomy is unsafe. Side-effecting and inferential actions are currently too close together in the contract surface.

### Tier 2 enforcement

This matters because structural validity does not prove safe behavior. The suite is ready to support behavior/authority/failure tests now.

### Fixture corpus

This matters because fixtures become the shared language for tests, simulation, replay, onboarding, and future generated nodes.

### Dependency policy

This matters because dependency outages and degraded modes are where autonomy systems silently lose trust.

### Event reliability classes

This matters because coordination and governance should not share the same delivery assumptions as telemetry.

### Shared provenance

This matters because provenance is the substrate for trust, replay, explainability, and governance.

---

## 4. Strategic plan in priority order

### Priority 1 — Unified node constitution artifact

A single top-level `node.constitution.yaml` was **explored and removed** from this repo; the role is largely covered today by **`docs/contracts/**`**, [`tools/l9_enrichment_manifest.yaml`](tools/l9_enrichment_manifest.yaml), and CI/audit gates.

**Next evolution (same intent):** either reintroduce one consolidated admission manifest *or* formally designate the manifest + `env-contract.yaml` + packet contracts as the joined “constitution” with a single digest step in CI.

**Expected effect**
- one admission-oriented view for GATE / node registration
- less drift between YAML packs and enforcement

### Priority 2 — Add an action authority model

For each action (enrich, discover, converge, enrich_and_sync, writeback), define:
- mutation class
- determinism class
- approval mode
- idempotency mode
- replay safety
- required evidence/provenance level
- dependency criticality
- event delivery class

Start with writeback first, because it is the highest-risk action and the one most in need of policy separation.

**Expected effect**
- safer autonomy
- cleaner routing policy
- clearer observability semantics
- better trust scoring by action type

### Priority 3 — Tier 2 enforcement tests (expand / harden)

**Status:** Many Tier-2 modules already exist under `tests/contracts/tier2/` (e.g. `test_enforcement_packet_runtime.py`, `test_enforcement_behavior.py`, `test_enforcement_authority.py`, `test_enforcement_dependency_failures.py`, `test_enforcement_replay_idempotency.py`, `test_enforcement_events.py`, `test_enforcement_provenance.py`).

Keep structural Tier 1 as-is; **deepen assertions, fixtures, and CI gating** so these tests become merge-blocking where appropriate.

Your existing manifest and fixture strategy already support this move.

**Expected effect**
- closes the biggest gap between contract intent and actual safety
- makes regressions visible before runtime
- creates the evidence needed for future autonomy scoring

### Priority 4 — Upgrade fixtures into a behavioral corpus

Expand fixtures from `minimal/full` into:
- valid
- invalid
- authority
- failure
- replay
- events
- convergence
- provenance
- dependency degraded modes

Also add fixture factories in test helpers so you can generate variations without explosion of static files.

**Expected effect**
- more test leverage per fixture
- better replay/regression capability
- reusable fuel for future nodes and workflows

### Priority 5 — Promote provenance into a shared contract primitive

Create shared schemas for:
- ProvenanceBundle
- EvidenceRef
- ProviderFingerprint
- PromptVersionRef
- DependencyFingerprint

Require those on:
- enrich response
- persistent results
- packet egress
- important event payloads

**Expected effect**
- replay gets stronger
- debugging becomes faster
- trust becomes computable
- future governance becomes practical

### Priority 6 — Add dependency degradation policy

For each dependency, declare:
- which actions need it
- whether its absence is fatal, degraded, or irrelevant
- fallback policy
- autonomy impact
- admission impact

Example:
- Redis may be optional for basic enrich response but mandatory for strict idempotency guarantees
- Postgres may be mandatory for convergence resumability
- Neo4j may be mandatory for enrich_and_sync
- CRM availability is mandatory for writeback
- Perplexity outage should trigger fallback or explicit fail state.

**Expected effect**
- safer degraded mode
- clearer runtime behavior
- stronger admission and rollout logic

### Priority 7 — Split event contracts by reliability class

Keep your current event schema work, but classify events into:
- telemetry
- coordination
- governance
- authoritative state change

Then define intended delivery requirements for each class, even if implementation reaches them in phases.

**Expected effect**
- safer downstream automation
- fewer silent coordination failures
- clearer division between observability and operational truth

### Priority 8 — Tighten the graph and prompt gaps

Two constitutionally weak areas remain:
- graph schema still contains TODOs and inferred shapes
- prompt registry still lacks exact extracted contracts.

Close both with:
- exact graph merge/write invariants
- exact prompt contracts or prompt grammar contracts

**Expected effect**
- much stronger determinism story
- more confidence in replay and drift detection
- better future template reuse

---

## 5. Expected compounding effects

If you execute priorities 1–8 in order, the compounding effects are strong:

### Constitution + authority model

Compounds because every future node can inherit the same governance language.

### Tier 2 enforcement + fixture corpus

Compounds because each new node can reuse the same behavioral test patterns and fixture organization.

### Provenance + dependency policy

Compounds because trust, replay, and degraded-mode handling become reusable platform capabilities, not EIE-specific hacks.

### Event classing + graph/prompt hardening

Compounds because cross-node composition becomes safer and faster as more nodes join the constellation.

### Templates + constitution

Compounds the most because this is the path from one documented node to a reproducible node family.

---

## 6. What this unlocks across workflows, pipelines, nodes, and scale

### Across workflows
- faster onboarding of new workflows because request/response/action/provenance behavior becomes predictable
- safer writeback and discovery flows because authority is explicit
- easier debugging because provenance and degraded-mode states are visible

### Across pipelines
- replayable enrichment/convergence pipelines
- stronger contract-aware CI
- dependency-aware rollout and fallback logic
- packet-level enforcement rather than ad hoc handler trust

### Across nodes
- shared constitution format
- reusable action taxonomy
- reusable event classes
- reusable provenance model
- reusable Tier 2 enforcement suite
- reusable templates for new nodes

### Across scale
- GATE can evolve toward policy-aware admission and routing
- trust/autonomy can be computed from test history + runtime attestation + dependency state
- future nodes can be generated faster and with less semantic drift
- the constellation becomes composable instead of artisanal

---

## Focused conclusion

The fastest path to immediate project leverage is not adding more contract files.

It is:
1. unify the contracts into a node constitution
2. define action authority semantics
3. add Tier 2 enforcement tests
4. evolve fixtures into a behavioral corpus
5. elevate provenance, degradation, and event reliability into shared platform primitives

Those moves have the highest:
- breadth of impact
- compounding effect
- speed to value
- reusability across nodes
- leverage across workflows and pipelines

They close the exact gap between where you are now — a strong descriptive node blueprint — and the finished state: a reusable, governed, enforceable constellation node substrate.
