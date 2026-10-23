# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: enrichment
# layer: [governance]
# tags: [L9_TEMPLATE, adr, ci, waiver]
# owner: platform
# status: active
# --- /L9_META ---

# ADR-003: safety-check Security Scan is Non-Blocking in CI

**Date**: 2026-04-01 | **Status**: Superseded 2026-10-23 (safety removed; see Amendment) | **Author**: Platform Team | **Waiver**: WAIVER-003 (retired)

## Context

The `safety check` tool overlaps significantly with `pip-audit` on the same PyPA advisory database. Running both provides marginal additional signal while doubling triage burden.

## Decision

The `security-safety-check` CI job is non-blocking. It serves as a secondary signal layer. pip-audit (WAIVER-002) is the primary security scan.

## Consequences

Positive: Reduces triage noise. Negative: None material — pip-audit covers the same advisory set.

## Review Cadence

Monthly review alongside pip-audit. Policy decision — does not expire.

## Amendment (2026-10-23): safety removed from CI

**Status**: Superseded by removal. WAIVER-003 is retired.

`safety` 3.8.1 (the latest release) requires `filelock<4.0`. filelock 3.32.7 carries CVE-2026-112907, CVE-2026-112913 and CVE-2026-112940, all fixed only in 4.2.1, so installing `safety` kept a vulnerable filelock in the CI environment and pip-audit failed on every run. `safety` also pulled in `nltk`, the sole reason for the `--ignore-vuln PYSEC-2026-3740` exception.

Since `safety` added no signal over pip-audit (the Context above), it is removed from `requirements-ci.txt` and both workflows (`ci.yml`, `pr-pipeline.yml`). Without it, filelock resolves to 4.2.x through pip-audit's own dependencies, nltk is absent, and `pip-audit --desc` passes with no exceptions. pip-audit remains the dependency-vulnerability scan.
