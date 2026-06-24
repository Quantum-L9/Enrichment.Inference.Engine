# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: enrichment
# layer: [governance]
# tags: [L9_TEMPLATE, agent, coderabbit, delta]
# owner: platform
# status: active
# token_estimate: 150
# ssot_for: [coderabbit-specific-delta]
# load_when: [coderabbit_review]
# references: [AGENTS.md, .coderabbit.yaml, AI_AGENT_REVIEW_CHECKLIST.md]
# --- /L9_META ---

# CODERABBIT.md — CodeRabbit-Specific Delta

**VERSION**: 1.2.0 | **SHA_BASELINE**: 358d15d | **LAST_REVIEWED**: 2026-05-24

> Delta-only file. Load [AGENTS.md](AGENTS.md) first. This file contains ONLY CodeRabbit-specific overrides.

## Primary Configuration

`.coderabbit.yaml` in repository root is the primary CodeRabbit configuration.
This file provides governance-layer context that supplements it.

## Paid tier — enablement checklist

1. **GitHub App** — [coderabbit.ai](https://coderabbit.ai) → install the CodeRabbit app on your org or this repository.
2. **Dashboard** — Grant repo access, pick plan, enable PR reviews (automatic reviews are driven by `.coderabbit.yaml`).
3. **Tune behavior** — Edit [`.coderabbit.yaml`](.coderabbit.yaml) (`reviews.profile`, `auto_review`, `path_filters`, `chat`). Schema: [CodeRabbit configuration reference](https://docs.coderabbit.ai/reference/configuration).

## Review Behavior

- **Auto-approve**: PRs where all phases of AI_AGENT_REVIEW_CHECKLIST.md pass with 0 CRITICAL, 0 HIGH violations.
- **Request changes**: Any CRITICAL violation (SEC-001, ARCH-001, C-10).
- **Escalation**: Tag `@cryptoxdog` on any C-01 chassis violation or credential exposure.

## Output Format

Use review comment templates from AI_AGENT_REVIEW_CHECKLIST.md.
Do NOT emit free-form prose for contract violations — always use the structured template.

## Paths to Skip

Skip review on: `docs/`, `readme/`, `reports/`, `*.json` (generated), `htmlcov/`, `coverage.xml`.
