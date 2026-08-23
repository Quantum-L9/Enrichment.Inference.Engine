<!-- Canonical org-wide PR template. Correct path: .github/pull_request_template.md
     inside the Quantum-L9/.github repo's own .github/ folder. A copy at repo ROOT
     does NOT propagate org-wide — GitHub only reads the nested path.
     See docs/AUDIT.md, finding #1. -->

## Problem

<!-- REQUIRED. The error, bug, or gap this fixes. Lead with the symptom a human saw.
     Paste the actual traceback, failing assertion, alert, or log line. -->

```
paste the error / failing output here, or delete this block and describe the gap
```

Closes #

## Fix

<!-- What you changed to make the problem above go away. Note alternatives you rejected and why. -->

## Risk

<!-- Pick exactly one. This routes how hard reviewers look. -->

- [ ] Low — additive, reversible, no data or contract change
- [ ] Medium — touches shared code, config, or a public interface
- [ ] High — breaking change, migration, IAM/network, or irreversible

Blast radius:
Rollback:

## Evidence

<!-- Show the problem is gone. Pasted output or a CI link. Not "tests pass". -->

```
$ pytest -q
$ ruff check . && pyright
```

## Gates

<!-- Leave unchecked if it does not apply, and say why on the line. An unchecked box
     with no reason blocks merge (see .github/workflows/pr-gates.yml). -->

- [ ] Regression test added that fails without this fix
- [ ] No secrets, tokens, or customer data in code, tests, fixtures, or logs
- [ ] `semgrep` clean, or findings triaged below
- [ ] New IAM / workflow permissions are least privilege and enumerated
- [ ] Third-party actions pinned to a full commit SHA
- [ ] Public interface change is documented and versioned
- [ ] Observability exists for the new path (metric, log, trace, or alert)

## Reviewer focus

<!-- Where to look hardest. Trade-offs accepted. Deferred follow-ups, with issue links. -->

## Changes by intent

<!-- YOU write this. One line per file you meant to touch, with the reason.
     This is the contract; the bot-generated list below is the actual diff.
     Any mismatch is flagged by CI — an unexplained file is usually a stray
     debug edit, a committed artifact, or scope creep.
     Delete the ADDED or MODIFIED heading if empty. Use `path — why`. -->

**Added**
- `path/to/new_file.py` — why this file needs to exist

**Modified**
- `path/to/existing.py` — what changed in it and why

**Deleted**
- `path/to/dead.py` — why it is safe to remove

## Files touched

<!-- Auto-filled by .github/workflows/pr-files.yml on every push. Do not edit by hand. -->

<!-- FILES-TOUCHED:START -->
_pending — the bot fills this in on push_
<!-- FILES-TOUCHED:END -->
