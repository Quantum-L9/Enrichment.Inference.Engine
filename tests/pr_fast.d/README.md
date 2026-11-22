# PR-fast lane test lists

On GitHub Actions, `make test` (`repo-test` in `Repo.mk`) runs a bounded
PR-fast selection, and that run is the only coverage SonarCloud reads for a
pull request. A change whose tests are not in the selection reaches SonarCloud
as uncovered, however well its own tests cover it.

The selection is the fixed list in `Repo.mk` **plus** every test path listed in
`tests/pr_fast.d/*.txt`:

- **One file per lane**, named `<campaign>-<lane>.txt`. Only that lane edits its
  file; no lane edits another lane's file. Files never conflict with each other.
- **One test path per line**, relative to the repository root. Blank lines and
  text after `#` are ignored.
- A listed path that does not exist **fails** the run; it never silently
  shrinks the selection.
- Each path runs at most once, however many files list it. A path that is
  also in the fixed list runs once (pytest drops duplicate paths).

This adds tests to the PR-fast run. It does not change the coverage floor, the
quarantine deselections, or the local `make test` (the full catalog).
