<!-- L9_PROTECTED_ROOT_PR -->
<!--
  The stamp above is required in the PR body when any additive_only root file
  is in the diff (ops/config/root-file-protection.json). make pr fills the
  Protected-root block at the end of this template. When no additive_only path
  is touched, the composer marks that section N/A.

  Prefer append-only edits. A rewrite/deletion also needs a commit line:
    ALLOW-ROOT-DELETION: <path> — <reason with proof of necessity>

  Machine-owned regions sit between a pair of HTML-comment markers named
  NAME:START and NAME:END (the real ones are below; they are never written
  inside another comment). make pr (Cursor-Governance
  ops/scripts/compose_pr_body.py) fills them, and the governance-pr workflow
  refreshes them on every push. Text outside the regions is yours and
  survives every refresh.
-->

## Files touched

<!-- Machine-owned: make pr and the governance-pr workflow refresh it from the
     diff on every push. Do not edit by hand. -->

<!-- FILES-TOUCHED:START -->
_pending — the bot fills this in on push_
<!-- FILES-TOUCHED:END -->

## Changes by intent

<!-- Machine-owned path list: one `path — why` line per diff row, refreshed on
     every push. Edit the text after a path; it survives refresh. -->

<!-- CHANGES-BY-INTENT:START -->
_pending — the composer lists every changed path from the diff_
<!-- CHANGES-BY-INTENT:END -->

## Problem

<!-- REQUIRED. The error, bug, or gap this fixes. Lead with the symptom a human saw.
     Paste the actual traceback, failing assertion, alert, or log line.
     Name an issue with a closing keyword only when this PR closes it. -->

```
paste the error / failing output here, or delete this block and describe the gap
```

## Type of Change

- [ ] Bug fix
- [ ] Feature / enhancement
- [ ] Refactor (no behavior change)
- [ ] Documentation
- [ ] CI / governance change
- [ ] Breaking change (see rollback below)

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

<!-- Show the problem is gone. Pasted output or a CI link. Not "tests pass".
     The EVIDENCE block is machine-owned (make pr receipts, each marked BOUND,
     STALE or UNVERIFIED against the head it describes). Add your own evidence
     below it. -->

<!-- EVIDENCE:START -->
_not measured — make pr records its receipts here_
<!-- EVIDENCE:END -->

## Gates

<!-- Leave unchecked. make pr checks a box only when a measurement exists
     and marks every other box not measured. -->

- [ ] Regression test added that fails without this fix
- [ ] No secrets, tokens, or customer data in code, tests, fixtures, or logs
- [ ] `semgrep` clean, or findings triaged below
- [ ] New IAM / workflow permissions are least privilege and enumerated
- [ ] Third-party actions pinned to a full commit SHA
- [ ] Public interface change is documented and versioned
- [ ] Observability exists for the new path (metric, log, trace, or alert)

## Semantic change

<!-- Applies when the diff touches semantics/, docs/semantic-foundation/, or a
     repository's authoritative product artifacts (product-topology.yaml,
     release-work/product-manifest.json, release-work/product-release-binding.yaml).
     Otherwise tick N/A; make pr does it from the measured diff. -->

- [ ] N/A — no canonical semantic artifact or release record changed
- Foundation / release candidate affected:
- Prior exact coordinate or source identity (artifact_id, digest, contract/binding id):
- New exact coordinate (when applicable):
- [ ] Transitive semantic dependents revalidated (`semantics/semantic_dependency_model.yaml`)
- [ ] `scripts/validate-semantics.py` result pasted under Evidence
- [ ] `docs/semantic-foundation/<version>/` manifest, `HASHES.sha256`, and `RELEASE_VALIDATION.md` updated when canonical bytes changed
- [ ] Admission / successor requirement checked (`L9-REVISION-001`: an admitted revision is immutable; material change needs a successor)

## Reviewer focus

<!-- Where to look hardest. Trade-offs accepted. Deferred follow-ups, with issue links. -->

## Protected-root

### Paths

<!-- One path per line. Must match the additive_only diff. Composer fills. -->

<!-- PROTECTED-ROOT-PATHS:START -->
_pending — the composer lists additive_only root paths from the diff_
<!-- PROTECTED-ROOT-PATHS:END -->

### Edit mode (pick one per path)

- [ ] **Append-only** — existing lines kept; only new lines added (no `ALLOW-ROOT-DELETION`)
- [ ] **Justified rewrite** — commit contains `ALLOW-ROOT-DELETION: <path> — <reason>`

### Why a root file

<!-- What cannot be done in a non-root path. Composer fills. -->

### Proof of necessity (rewrites only)

<!-- Issue, failing gate, or law citation. Empty if every path is append-only. -->
