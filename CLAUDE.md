# CLAUDE.md — Claude-Specific Instructions

> **Primary document**: `AGENTS.md` — read that first.
> This file contains Claude-specific additions only.

---

## Loading Order

1. `AGENTS.md` — contracts, tiers, active transport bundle
2. `ARCHITECTURE.md` — live SDK runtime topology
3. `REPO_MAP.md` — directory ownership and boundaries
4. `EXECUTION_FLOWS.md` — runtime paths
5. This file — Claude-specific guidance

---

## Output Guidelines

### Banned Phrases

Do NOT use vague language. Be specific.

| Banned | Use Instead |
|--------|-------------|
| "best practices" | Cite the specific contract or invariant |
| "as needed" | Specify the exact condition |
| "you may want to" | Make a binary recommendation |
| "consider using" | State whether required or optional per contract |
| "it depends" | State which contract governs the decision |
| "generally speaking" | Reference the exact rule |

### Transport Precision

- `/v1/execute` is owned by the SDK runtime, not local `chassis/router.py`
- `chassis/envelope.py`, `chassis/router.py`, and `chassis/registry.py` are deprecated compatibility artifacts
- The active transport/runtime bundle is:
  - `app/main.py`
  - `app/api/v1/chassis_endpoint.py`
  - `app/services/chassis_handlers.py`
  - `app/engines/orchestration_layer.py`
  - `app/engines/handlers.py`
  - `app/engines/graph_sync_client.py`

Do not describe deprecated local chassis dispatch as if it were the live production path.

---

## PR Review Format

### For Violations

```text
CONTRACT C-{N} VIOLATION — {rule-name}
File: {path} Line: {line}
Found: {offending_code}
Required: {corrected_code}
```

### For Approvals

```text
All active contracts verified. No violations found.
Tier: T{N} change — {0/1/2} reviewers required.
```

---

## CI Response

| Failed Job     | Action                                        |
| -------------- | --------------------------------------------- |
| `validate`     | Fix syntax/YAML first                         |
| `lint-ruff`    | Run `make agent-fix`                          |
| `lint-mypy`    | Log warning only if waiver applies            |
| `test`         | Fix tests, ensure coverage >= 60%             |
| `compliance-*` | Fix the violation                             |
| `contracts`    | Fix the active contract manifest / references |
| `security-*`   | Log warning only if waiver applies            |

---

## Ruff Ignores

Do not add inline `# noqa` for globally ignored rules in `pyproject.toml`.
Honor the frozen ignore list instead.

---

## Key Commands

```bash
make agent-check  # Full 7-gate validation
make agent-fix    # Auto-fix lint/format
make test         # Run tests
make verify       # Contract verification
```

---

## Claude-Specific Review Bias

When transport/runtime files are touched:

* verify C-13 lockstep
* verify C-21 SDK ingress ownership
* verify no PR reintroduces production reliance on deprecated chassis dispatch
* verify docs and manifest stay synchronized with runtime truth

<!-- BEGIN L9 FORMATTER OWNERSHIP (generated — do not edit) -->

## Formatter ownership

Workspace class: `biome_default` — Default for every governed workspace: Biome owns JS/TS/JSON, VS Code JSON language features owns JSONC (the Biome extension cannot format jsonc), Ruff owns Python, Prettier owns Markdown (format-on-save off so governance docs do not churn).

Exactly one formatter owns each language. Do not reformat a file with a tool other than its owner, and do not add config for a competing formatter: the result is a diff that churns on every save.

| Languages | Owner | Note |
|---|---|---|
| `javascript`, `javascriptreact`, `typescript`, `typescriptreact`, `json` | **biome** | bound by the governed IDE profile |
| `jsonc` | **vscode-json** | bound by the governed IDE profile |
| `python` | **ruff** | bound by the governed IDE profile |
| `markdown` | **prettier** | bound by the governed IDE profile |

Generated from `environment/ide/policy.json` in the governance clone by `ops/scripts/adapters/agentdocs.sh`. Edit the policy, not this block.

<!-- END L9 FORMATTER OWNERSHIP -->
