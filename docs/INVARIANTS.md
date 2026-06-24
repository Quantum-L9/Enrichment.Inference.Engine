# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: enrichment
# layer: [governance]
# tags: [L9_TEMPLATE, invariants, architecture, enforcement]
# owner: platform
# status: active
# token_estimate: 2834
# ssot_for: [invariants, architectural-rules, process-rules]
# load_when: [architectural_questions, invariant_lookup, refactor]
# references: [AGENTS.md, CI_WHITELIST_REGISTER.md]
# --- /L9_META ---

# INVARIANTS.md — Immutable Architectural Rules

**VERSION**: 2.2.0 | **SHA_BASELINE**: 358d15d | **LAST_REVIEWED**: 2026-04-11

> These rules must hold across ALL repository states.
> Violation of any CRITICAL invariant is grounds for immediate PR rejection.
> Gate sequence definition: see [AGENTS.md](AGENTS.md) (`make agent-check`).

---

## Contract to Invariant Cross-Reference

| AGENTS.md Contract | Invariant | Status |
|---|---|---|
| C-01 (API/engine boundary) | INV-2 | COVERED |
| C-02 (handler contract) | INV-6 | PARTIAL |
| C-03 (Tenant scoping) | INV-3 | GAP |
| C-04 (structlog only) | INV-4 | PARTIAL |
| C-07 (Cypher injection) | INV-7 | PARTIAL |
| C-09 (L9_ prefix) | INV-20 | PARTIAL |
| C-10 (no hardcoded creds) | INV-12 | COVERED |
| C-11 (transport immutability) | INV-15 | PARTIAL |
| C-13 (active transport bundle lockstep) | INV-1 | GAP |
| C-15 (coverage >= 60%) | INV-9 | COVERED |
| C-16 (Python 3.12+) | INV-11 | COVERED |
| C-18 (frozen ruff ignores) | INV-17 | GAP |
| C-20 (L9_META header) | INV-10 | PARTIAL |
| C-21 (SDK owns `/v1/execute`) | INV-1 | GAP |

---

## Part A — Architectural Invariants

### INV-1: SDK Transport Ingress Governance
**Severity**: CRITICAL | **Enforcement**: GAP — human review + static scans
**Rule**: Production transport ingress is owned by the SDK runtime in `app/main.py`. `/v1/execute` must not be implemented or re-routed through deprecated local chassis dispatch artifacts. The active transport/runtime bundle is:
- `app/main.py`
- `app/api/v1/chassis_endpoint.py`
- `app/services/chassis_handlers.py`
- `app/engines/orchestration_layer.py`
- `app/engines/handlers.py`
- `app/engines/graph_sync_client.py`

### INV-2: API / Engine Import Boundary
**Severity**: CRITICAL | **Enforcement**: COVERED
**Rule**: Pure engine modules under `engine/` and engine-only portions of `app/engines/` MUST NOT import FastAPI.

### INV-3: Tenant Scoping Completeness
**Severity**: CRITICAL | **Enforcement**: GAP
**Rule**: Every Neo4j query that reads or writes domain data MUST include tenant scoping.

### INV-4: Observability Config Locality
**Severity**: HIGH | **Enforcement**: PARTIAL
**Rule**: structlog configuration belongs to runtime/bootstrap code, not engine-only modules.

### INV-5: Infrastructure Immutability
**Severity**: HIGH | **Enforcement**: GAP
**Rule**: Docker/compose/deploy files require elevated review.

### INV-6: Handler Contract Stability
**Severity**: CRITICAL | **Enforcement**: PARTIAL
**Rule**: SDK-registered handlers must conform to the production handler contract. Optional SDK-supported context parameters must be explicitly documented where used. No undocumented signature drift.

### INV-7: Cypher Injection Prevention
**Severity**: CRITICAL | **Enforcement**: PARTIAL
**Rule**: All dynamic Cypher labels must use `sanitize_label()`. All values must be parameterized.

### INV-8: Gate-Then-Score Architecture
**Severity**: CRITICAL | **Enforcement**: PARTIAL
**Rule**: Gate compilation must precede scoring; these stages must not be merged or reversed.

### INV-9: Coverage Threshold >= 60%
**Severity**: HIGH | **Enforcement**: COVERED
**Rule**: Test coverage must never drop below 60%.

### INV-10: L9_META Presence
**Severity**: MEDIUM | **Enforcement**: PARTIAL
**Rule**: Template-managed files must contain valid L9_META headers.

### INV-11: Python Version 3.12+
**Severity**: CRITICAL | **Enforcement**: COVERED
**Rule**: The codebase targets Python 3.12+.

### INV-12: Forbidden eval/exec/pickle
**Severity**: CRITICAL | **Enforcement**: PARTIAL
**Rule**: eval(), exec(), compile(), and pickle.loads() are forbidden in production code.

### INV-13: YAML SafeLoader
**Severity**: CRITICAL | **Enforcement**: PARTIAL
**Rule**: All YAML loading must use `yaml.safe_load()`.

### INV-14: Zero-Stub Protocol
**Severity**: HIGH | **Enforcement**: PARTIAL
**Rule**: No pass, ..., or TODO may exist in committed production code outside allowed contexts.

### INV-15: SDK Transport Immutability
**Severity**: CRITICAL | **Enforcement**: PARTIAL
**Rule**: `TransportPacket` and SDK transport objects MUST NOT be mutated after construction. Deprecated local chassis dict envelopes are not part of the production boundary and must not be treated as the constitutional transport contract.

---

## Part B — Process Invariants

### INV-16: Directory Structure Stability
**Severity**: HIGH | **Enforcement**: GAP
**Rule**: Top-level directories may not be added or renamed without architecture approval.

### INV-17: Ruff Ignore List Immutability
**Severity**: HIGH | **Enforcement**: GAP
**Rule**: The ruff ignore list in `pyproject.toml` is frozen.

### INV-18: Test Marker Completeness
**Severity**: MEDIUM | **Enforcement**: PARTIAL
**Rule**: Tests must follow repo marker conventions.

### INV-19: Agent Check Gate Sequence
**Severity**: HIGH | **Enforcement**: COVERED
**Rule**: The 7-gate `make agent-check` sequence is mandatory pre-commit verification.

### INV-20: Environment Variable Prefix Convention
**Severity**: MEDIUM | **Enforcement**: PARTIAL
**Rule**: Application-level environment variables use `L9_` prefix except explicitly allowed infrastructure-standard names.

---

## Deprecated Compatibility Artifacts

These files may exist in the repository but are excluded from the active transport constitution:

- `chassis/envelope.py`
- `chassis/router.py`
- `chassis/registry.py`

Any PR that attempts to reintroduce them into production dispatch violates INV-1.

---

## Enforcement Coverage Summary

| Status | Count | Invariants |
|---|---|---|
| COVERED | 4 | INV-2, INV-9, INV-11, INV-19 |
| PARTIAL | 11 | INV-4, INV-6, INV-7, INV-8, INV-10, INV-12, INV-13, INV-14, INV-15, INV-18, INV-20 |
| GAP | 5 | INV-1, INV-3, INV-5, INV-16, INV-17 |
