# CI_WHITELIST_REGISTER.md — CI Gates & Waivers

## Merge-Blocking Gates

All blocking gates must pass to merge.

| Gate | CI Job | What It Checks |
|------|--------|----------------|
| 1 | `validate` | Python syntax, YAML validation |
| 2 | `lint-ruff` | `ruff check .` |
| 3 | `lint-format` | `ruff format --check .` |
| 4 | `test` | pytest (coverage >= 60%) |
| 5 | `compliance-architecture` | Runtime/API/engine structure and SDK transport ownership |
| 6 | `compliance-field-names` | Snake_case, no aliases, no naming drift |
| 7 | `compliance-imports` | Import resolution and no deprecated dispatch reliance |
| 8 | `compliance-banned-patterns` | No eval/exec/compile, no engine FastAPI drift, no deprecated chassis dispatch in production path |
| 9 | `contracts` | `verify_contracts.py` — active contract manifest integrity |

---

## Non-Blocking Jobs (Waivers)

These jobs run but failures do not block merge unless a waiver is removed.

| Job | Waiver ID | Reason |
|-----|-----------|--------|
| `lint-mypy` | WAIVER-001 | Progressive type annotation rollout |
| `security-pip-audit` | WAIVER-002 | Requires human triage |
| `security-safety-check` | WAIVER-003 | Secondary to pip-audit |
| `security-bandit` | WAIVER-004 | Human triage required |
| `coverage-codecov-upload` | WAIVER-005 | Network/token issue, not code quality |

---

## Agent Response to CI Failures

| Failed Job | Action |
|------------|--------|
| `validate` | Fix syntax or YAML first |
| `lint-ruff` | Run `make agent-fix`, re-run |
| `lint-format` | Run `make agent-fix`, re-run |
| `lint-mypy` | Log warning only if WAIVER-001 still applies |
| `test` | Fix failing tests and restore coverage |
| `compliance-architecture` | Restore SDK-owned transport/runtime structure |
| `compliance-field-names` | Replace banned naming patterns |
| `compliance-imports` | Fix unresolved imports and remove deprecated dispatch imports |
| `compliance-banned-patterns` | Remove banned runtime/engine patterns |
| `contracts` | Fix manifest SHA/reference drift, then run `make verify` |
| `security-*` | Log warning if a waiver still applies; otherwise fix |

---

## Quick Fix Commands

```bash
make agent-fix
make agent-check
make verify
```

---

## CI Workflows

| Workflow                   | Trigger        | Purpose                            |
| -------------------------- | -------------- | ---------------------------------- |
| `ci.yml`                   | push, PR       | Main pipeline                      |
| `compliance.yml`           | PR             | Structural/compliance enforcement  |
| `l9-constitution-gate.yml` | PR             | Governance / contract verification |
| `l9-contract-control.yml`  | PR             | Selective contract gating          |
| `pr-pipeline.yml`          | PR             | PR validation                      |
| `supply-chain.yml`         | push           | Dependency security                |
| `codeql.yml`               | push, schedule | CodeQL analysis                    |
| `gitguardian.yml`          | push           | Secret detection                   |

---

## Coverage Requirements

* **Minimum**: 60%
* **Target**: 80%+ for engine/runtime-critical modules

---

## Related Documents

* `AGENTS.md` — contract list
* `AI_AGENT_REVIEW_CHECKLIST.md` — PR review checks
* `.github/workflows/` — workflow definitions
* `tools/l9_enrichment_manifest.yaml` — active contract manifest
