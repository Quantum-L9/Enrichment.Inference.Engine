<!-- Agent / chore PR body. make pr and auto-seed write this variant so
     Enforce-PR-Policies does not fail generated PRs. Humans use
     pull_request_template.md. -->

## Problem

Agent or chore change. Evidence is the gate receipt or Actions run linked below.

Closes #

## Fix

Seed / pack / generated change. See Changes by intent.

## Risk

- [x] Low — additive, reversible, no data or contract change
- [ ] Medium — touches shared code, config, or a public interface
- [ ] High — breaking change, migration, IAM/network, or irreversible

Blast radius: seed dests only (missing-only; customized files kept).
Rollback: revert this PR; consumers keep files already written.

## Evidence

<!-- Machine-fill from .l9/pr/gate-receipt.json or the Actions run URL. -->

```
n/a — because this is a generated seed / chore PR; see the Actions run
```

https://github.com/${GITHUB_REPOSITORY}/actions

## Gates

- [ ] Regression test added that fails without this fix — n/a — because seed / chore, no product behavior change
- [x] No secrets, tokens, or customer data in code, tests, fixtures, or logs
- [ ] `semgrep` clean, or findings triaged below — n/a — because pack seed does not change product code
- [ ] New IAM / workflow permissions are least privilege and enumerated — n/a — because no new write scopes
- [ ] Third-party actions pinned to a full commit SHA — n/a — because caller pins are unchanged or SHA-pinned
- [ ] Public interface change is documented and versioned — n/a — because no public interface change
- [ ] Observability exists for the new path (metric, log, trace, or alert) — n/a — because no new runtime path

## Reviewer focus

Confirm dests are missing-only / replaceable-stock, and default categories
still omit LICENSE, FUNDING, labels.yml, and on-org-update.yml.

## Changes by intent

<!-- Generate from `git diff --name-only` in the publish path. -->

**Added**
- seed dests — missing-only org pack

**Modified**
- n/a

**Deleted**
- n/a

## Files touched

<!-- FILES-TOUCHED:START -->
_pending — the bot fills this in on push_
<!-- FILES-TOUCHED:END -->
