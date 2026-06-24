# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: enrichment
# layer: [governance]
# tags: [L9_TEMPLATE, agent, review, checklist, pr]
# owner: platform
# status: active
# token_estimate: 1570
# ssot_for: [pr-review-protocol, review-decision-matrix, comment-templates]
# load_when: [pr_review, code_review]
# references: [AGENTS.md, CI_WHITELIST_REGISTER.md, INVARIANTS.md]
# --- /L9_META ---

# AI_AGENT_REVIEW_CHECKLIST.md — PR Review Checklist

**VERSION**: 2.1.0 | **SHA_BASELINE**: 358d15d | **LAST_REVIEWED**: 2026-04-11

> Load [AGENTS.md](AGENTS.md) before this file for contracts, tiers, and patterns.

---

## Review Decision Matrix

Apply this verdict BEFORE writing any inline comments.

| CRITICAL violations | HIGH violations | Verdict |
|---|---|---|
| 0 | 0 | APPROVE (add suggestions as non-blocking comments) |
| 0 | 1-2 | REQUEST_CHANGES — must fix before merge |
| 0 | 3+ | REQUEST_CHANGES — tag @platform-team |
| 1+ | any | REQUEST_CHANGES — tag @platform-team |
| SEC-001 or ARCH-001 or ARCH-003 | any | REQUEST_CHANGES — tag @platform-team + add security/architecture label |

---

## Phase 1 — STOP: Merge-Blocking Violations (Check First)

- [ ] C-01 / ARCH-001: `from fastapi import` outside allowed modules?
- [ ] C-06 / SEC-002/003: eval(, exec(, compile(, pickle.loads( present?
- [ ] C-07 / SEC-001: Cypher f-string without sanitize_label()?
- [ ] C-08 / SEC-007: yaml.load( without SafeLoader?
- [ ] C-10: Hardcoded API key, token, or credential?
- [ ] C-11: SDK transport objects mutated in place after construction?
- [ ] C-13: Active transport/runtime bundle drift — changes to `app/main.py`, `app/api/v1/chassis_endpoint.py`, `app/services/chassis_handlers.py`, `app/engines/orchestration_layer.py`, `app/engines/handlers.py`, or `app/engines/graph_sync_client.py` without paired updates to the active bundle?
- [ ] C-21: Does this PR reintroduce production reliance on deprecated `chassis/envelope.py`, `chassis/router.py`, or `chassis/registry.py`?
- [ ] C-16: Python less than 3.12 syntax or backport import present?

---

## Phase 2 — STOP: Contract Violations

- [ ] C-02: Handler signatures match production contract (`async def handle_*(tenant: str, payload: dict[str, Any]) -> dict[str, Any]`) unless explicitly documented SDK extension is used?
- [ ] C-03: All Neo4j queries include tenant scoping?
- [ ] C-04: No print() in app/ or engine/ (structlog only)?
- [ ] C-05: No Optional[, List[, Dict[ — use T or None, list[T], dict[K,V]?
- [ ] C-09: New env vars use L9_ prefix or documented exception?
- [ ] C-12: No Field(alias=...) in Pydantic models?
- [ ] C-14: New SDK action registration has handler + test in same PR?
- [ ] C-17: No camelCase Python field names?
- [ ] C-20: New template-managed files have L9_META header?

---

## Phase 3 — WARN: Code Quality

- [ ] All new public functions have type annotations and docstrings?
- [ ] No bare except: clauses?
- [ ] No pass or ... as function bodies in non-test, non-protocol files?
- [ ] No hardcoded magic numbers (use named constants)?
- [ ] structlog used correctly (key=value pairs, not positional args)?

---

## Phase 4 — VERIFY: Tests

- [ ] Tests added/updated for all changed logic?
- [ ] All tests have at least one pytest marker where required?
- [ ] Integration tests do not depend on external services without mocking or declared integration harness?
- [ ] Coverage does not drop below 60%?

---

## Phase 5 — INFORM: PR-Type-Specific Checks

### New Feature PR
- [ ] T-tier assessed and appropriate reviewers assigned?
- [ ] New env vars documented in config docs / `.env.example`?
- [ ] New files listed in repo governance docs when required?

### Bug Fix PR
- [ ] Root cause identified in PR description?
- [ ] Regression test added for the exact failure case?

### Dependency Update PR
- [ ] Import restriction compliance preserved?
- [ ] SDK runtime / Gate transport compatibility preserved?

### CI Workflow Change PR
- [ ] No merge-blocking gate converted to non-blocking without explicit waiver?

### Documentation-Only PR
- [ ] No doc changes contradict AGENTS.md / INVARIANTS.md / runtime truth?

### Config Change PR
- [ ] Ruff ignore list unchanged?
- [ ] Coverage threshold not lowered?
- [ ] Transport/runtime bundle references remain correct?

---

## Review Comment Templates

### Contract Violation
```text
CONTRACT C-{N} VIOLATION — {rule-name}
File: {path} Line: {line}
Found: {offending_code}
Required: {corrected_code}
Evidence: AGENTS.md Architectural Contracts (C-{N})
````

### Tier Escalation Required

```text
TIER ESCALATION — T{N} change requires {0/1/2} reviewer(s)
File: {path}
Reason: Modifying {file} is a T{N} operation per AGENTS.md Autonomy Tiers.
Action: Add [NEEDS-2-REVIEWERS] to PR title and request additional review.
```

### Non-Blocking Suggestion

```text
SUGGESTION (non-blocking) — {title}
File: {path} Line: {line}
Consider: {suggestion}
Reference: {contract_or_invariant_id}
```

### Approve Comment

```text
All active contracts verified — no violations found.
Tier assessment: T{N} — {0/1/2} reviewer(s) required.
make agent-check gates: [confirmed passing / please confirm before merge].
```
