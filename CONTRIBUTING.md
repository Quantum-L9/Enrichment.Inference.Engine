# Contributing to Quantum-L9

## Live activation (do this once per machine)

Governance loads automatically. Do **not** clone Cursor-Governance into a
consumer workspace root, and do **not** create whole-directory Cursor
rules, skills, or commands symlinks — that is the retired v2 ritual and
it creates a second governance tree.

What actually wires:

1. **sessionStart** — `ops/hooks/session_start_bootstrap.sh` activates the
   GitHub tip at `$HOME/.cursor-governance` (fast-forward or clone+swap).
2. **`l9-governance` plugin** — `~/.cursor/plugins/local/l9-governance` →
   the governance clone. Cursor discovers `rules/`, `skills/`, and
   `commands/` under the plugin root.
3. **`.cursor-commands`** — consumers only: a symlink to
   `$HOME/.cursor-governance`. The SSOT clone must never self-alias.

If a consumer workspace is missing those links:

```bash
bash "$HOME/.cursor-governance/ops/scripts/ensure_workspace_wired.sh" "$(pwd)"
```

Read [CANONICAL_LAW.md](https://github.com/Quantum-L9/Cursor-Governance/blob/main/CANONICAL_LAW.md)
for the symlink contract and anti-patterns.

## How an agent ships

1. Local commits on a feature branch (L4 local autonomy).
2. Run `kernels/Recursive Alignment.md` then `kernels/Validate & Repair.md`.
3. Publish **only** with:

```bash
PR_REMEDIATE=0 make pr
```

Do **not** `git push`, `gh pr create`, or `gh pr edit` to reach GitHub.
`make pr` runs the checkers; the alternatives skip them.

Campaign PRs set `PR_BASE=origin/campaign/<campaign_id>` and never target
`main`. Merge is a separate `/l9-pr-remediation` (Converge) step — opening
a PR is not merge authorization.

## CI gates

Consumer CI is the org `l9-ci-pack` (missing-only seed):

| Gate | When it runs |
| --- | --- |
| Biome | Always (JS/TS/JSON). Idle and green when the tree has no matching files. |
| Python lint + `Python Test Suite` | Only when `pyproject.toml` or `requirements.txt` exists. |
| Node typecheck + `Node Test Suite` | Only when a root `package.json` exists. |
| L9 Analysis | Semgrep + SDK publish. Fails with `governance-pack-missing` if `.github/governance/` is absent. |
| Governance caller | PR body / issue triage via `governance.yml@v1`. |

Do not invent a second `biome.json` or a competing `ci.yml`. Extra Biome
excludes append to `files.includes` only.

## Branch naming and commits

- Branches: `feat/<scope>`, `fix/<scope>`, `chore/<scope>`, `docs/<scope>`
- Commits: Conventional Commits — `feat(scope): message`
- Blast-radius paths (`.github/`, `infra/`, `SECURITY.md`, `CODEOWNERS`)
  require the CODEOWNERS team plus the extra reviewer on those paths only.

## Kernel authoring (l9-ci-core contributors only)

- Kernels must use `on: workflow_call` only.
- Never reference `@main` from thin callers.
- See [workflow-interface-registry.yml](https://github.com/Quantum-L9/.github/blob/main/workflow-interface-registry.yml).
