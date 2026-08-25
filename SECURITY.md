---
severity_sla:
  critical: { cvss: "9.0-10.0", acknowledge: "24h", patch_target: "7d" }
  high: { cvss: "7.0-8.9", acknowledge: "48h", patch_target: "14d" }
  medium: { cvss: "4.0-6.9", acknowledge: "48h", patch_target: "30d" }
  low: { cvss: "0.1-3.9", acknowledge: "5 business days", patch_target: "next release" }
routing:
  vulnerability: security-advisory
  conduct: CODE_OF_CONDUCT.md
  ci_seed_failure: ci-failure.yml
---

# Security Policy

## Scope

This policy applies to **this repository**. The seeder writes one
`SECURITY.md` per consumer; do not keep a second competing file. A local
copy replaces org inheritance entirely — there is no merge.

## Out of Scope

Vulnerabilities requiring physical access, social engineering of maintainers, or
issues in third-party dependencies without a demonstrated exploit path against
this repository — report those upstream instead.

## Reporting a Vulnerability

**Do NOT open a public GitHub issue for security vulnerabilities.**

Report privately via [this repository's Security Advisory form](https://github.com/Quantum-L9/Enrichment.Inference.Engine/security/advisories/new).
The org seeder rewrites that URL to `$GITHUB_REPOSITORY/security/advisories/new`
for the consumer being seeded. `ISSUE_TEMPLATE/config.yml` `contact_links` use
the same URL.

Conduct reports go to `CODE_OF_CONDUCT.md` / `gov-violation.yml`. CI seed
failures go to `ci-failure.yml` or `seed-ci-failure.yml`.

Include:

- Affected repository and version/SHA
- Vulnerability type and CVSS score estimate (see guidance below)
- Reproduction steps (minimal reproducer preferred)
- Potential impact assessment
- Any proposed mitigations

## Response SLA

| Severity                 | Acknowledge     | Patch Target       |
| ------------------------ | --------------- | ------------------ |
| Critical (CVSS 9.0–10.0) | 24 hours        | 7 days             |
| High (CVSS 7.0–8.9)      | 48 hours        | 14 days            |
| Medium (CVSS 4.0–6.9)    | 48 hours        | 30 days            |
| Low (CVSS 0.1–3.9)       | 5 business days | Next release cycle |

## CVSS Scoring Guidance for Reporters

Use [CVSS v3.1 Calculator](https://www.first.org/cvss/calculator/3.1) to estimate severity.
Key vectors: Attack Vector, Attack Complexity, Privileges Required, User Interaction, Scope, CIA Impact.

## Automated Security Controls

All repositories use:

- **gitleaks** — secret scanning on every commit
- **Semgrep** — SAST via `l9-analysis.yml` (stack-selected rulesets)
- **Dependabot** — github-actions SHA-pin freshness (no pip/npm unless added)
- **OpenSSF Scorecard** — supply-chain security posture scoring where enabled

## Disclosure Policy

Quantum-L9 follows coordinated disclosure. We request 90 days to remediate before public disclosure.
After the patch is released, we will publish a GitHub Security Advisory crediting the reporter (unless anonymity is requested).
