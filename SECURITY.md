# Security Policy

## Scope

This policy applies to all repositories in the **Quantum-L9** GitHub organization,
including internal tooling (`l9-*` cartridges), infrastructure-as-code, and CI/CD
workflows. This file is the **single canonical source**, inherited org-wide from
`Quantum-L9/.github` — individual repos MUST NOT maintain a competing `SECURITY.md`;
link here instead. (One local copy makes that repo ignore this file entirely; there
is no merging.)

## Out of Scope

Vulnerabilities requiring physical access, social engineering of maintainers, or
issues in third-party dependencies without a demonstrated exploit path against
Quantum-L9 systems specifically — report those upstream instead.

## Reporting a Vulnerability

**Do NOT open a public GitHub issue for security vulnerabilities.**

Report vulnerabilities privately via [GitHub Security Advisories](https://github.com/Quantum-L9/.github/security/advisories/new).

Include:
- Affected repository and version/SHA
- Vulnerability type and CVSS score estimate (see guidance below)
- Reproduction steps (minimal reproducer preferred)
- Potential impact assessment
- Any proposed mitigations

## Response SLA

| Severity | Acknowledge | Patch Target |
|---|---|---|
| Critical (CVSS 9.0–10.0) | 24 hours | 7 days |
| High (CVSS 7.0–8.9) | 48 hours | 14 days |
| Medium (CVSS 4.0–6.9) | 48 hours | 30 days |
| Low (CVSS 0.1–3.9) | 5 business days | Next release cycle |

## CVSS Scoring Guidance for Reporters

Use [CVSS v3.1 Calculator](https://www.first.org/cvss/calculator/3.1) to estimate severity.
Key vectors: Attack Vector, Attack Complexity, Privileges Required, User Interaction, Scope, CIA Impact.

## Security Packages

The [`l9-assurance`](https://github.com/Quantum-L9/l9-assurance) monorepo provides:
- [`l9-agent-security-testkit`](https://github.com/Quantum-L9/l9-assurance/tree/main/packages/l9-agent-security-testkit) — agent-layer security test utilities
- [`l9-security-testkit`](https://github.com/Quantum-L9/l9-assurance/tree/main/packages/l9-security-testkit) — general security testing framework

## Automated Security Controls

All repositories use:
- **gitleaks** — secret scanning on every commit
- **Bandit + Semgrep** — Python SAST
- **pip-audit / npm audit** — dependency vulnerability scanning
- **Dependabot** — automated dependency updates with SHA pinning via `ratchet`
- **OpenSSF Scorecard** — supply-chain security posture scoring

## Disclosure Policy

Quantum-L9 follows coordinated disclosure. We request 90 days to remediate before public disclosure.
After the patch is released, we will publish a GitHub Security Advisory crediting the reporter (unless anonymity is requested).
