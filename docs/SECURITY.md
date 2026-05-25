# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 2.x     | ✅ Active (see `pyproject.toml` / `app/main.py` for current package and runtime version) |
| 1.x     | ❌ EOL     |

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Report vulnerabilities privately to:

- **Email:** ib@scrapmanagement.com
- **Subject:** `[SECURITY] Enrichment.Inference.Engine — <brief description>`

### What to include
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (optional)

### Response timeline
- **Acknowledgement:** within 48 hours
- **Triage:** within 5 business days
- **Fix / disclosure:** coordinated with reporter

## Scope

### In scope
- Remote code execution via enrichment pipeline inputs
- Secret/credential exposure through API responses or logs
- Authentication bypass
- Injection attacks (prompt injection, YAML injection, path traversal)
- Dependency vulnerabilities in production dependencies

### Out of scope
- Issues in development dependencies only
- Social engineering
- Denial of service via rate limiting

## AI-Specific Security Notes

This project processes external data through LLM APIs (Perplexity sonar-reasoning).
Prompt injection via enrichment inputs is treated as a critical vulnerability.
All LLM outputs are validated against the signal schema before being written to CRM records.
