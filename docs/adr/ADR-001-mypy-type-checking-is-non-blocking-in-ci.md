# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: enrichment
# layer: [governance]
# tags: [L9_TEMPLATE, adr, ci, waiver]
# owner: platform
# status: active
# --- /L9_META ---

# ADR-001: MyPy Type Checking is Non-Blocking in CI

**Date**: 2026-04-01 | **Status**: Superseded 2026-10-31 (WAIVER-001 retired; see Amendment) | **Author**: Platform Team | **Waiver**: WAIVER-001 (retired)

## Context

The codebase was undergoing progressive type annotation adoption. At the time of
this decision, MyPy reported errors due to incomplete annotations in legacy
modules and third-party stubs. Making MyPy blocking would have prevented all
PRs from merging during the rollout period.

## Original Decision

The `lint-mypy` CI job was non-blocking. MyPy failures were reported as
informational output. PRs could merge when all other blocking gates passed.

## Original Consequences

Positive: unblocked development during annotation rollout. Negative: type
errors could accumulate. Mitigation: MyPy error count was tracked in CI output.
The gate would convert to blocking when the rollout debt was eliminated.

## Amendment (2026-10-31): strict zero-debt MyPy gate

**Status**: WAIVER-001 retired. MyPy is blocking in local and GitHub
validation.

The rollout debt is eliminated in the integrated remediation stack. `make
type-check` propagates every MyPy finding; both `ci.yml` and
`pr-pipeline.yml` do the same. There is no strict-mode opt-in,
manual-dispatch toggle, shell fallback, baseline exception, or ledger-update
mode that can convert a finding into success.

`tools/mypy_baseline.txt` remains only as a zero-debt sentinel. It may contain
comments, but it cannot contain findings; `tools/mypy_ratchet.py` rejects any
recorded entry. Findings must be corrected, not suppressed, waived, or
recorded as debt.

## Review Cadence

Review this policy when MyPy scope or configuration changes. The invariant is
unchanged: the recorded-debt ledger remains empty and every MyPy finding fails
the gate.
