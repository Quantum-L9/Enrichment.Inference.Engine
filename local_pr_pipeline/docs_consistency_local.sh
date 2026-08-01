#!/usr/bin/env bash
# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: enrichment
# layer: [ci, governance, docs]
# tags: [L9_TEMPLATE, ci, docs, consistency]
# owner: platform
# status: active
# --- /L9_META ---
#
# docs_consistency_local.sh — Documentation consistency checks.
#
# Referenced by .github/workflows/pr-pipeline.yml ("Run docs consistency checks"
# step in the docs job). The script was missing from the repo, so the docs gate
# failed on every PR regardless of the change. This restores it.
#
# It runs the repository's documentation checks and fails if any fails:
#   1. Required top-level governance docs exist.
#   2. Root-level markdown links resolve (delegated to docs_link_check_local.py).
#
# Uses only the Python standard library so it runs in the minimal docs CI job
# (no third-party deps installed). KB YAML validation is intentionally NOT run
# here; it is covered by the validate and compliance jobs, which install PyYAML.
#
# Exit 0 when everything is consistent, non-zero otherwise.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON="${PYTHON:-python3}"

cd "$REPO_ROOT"

FAILED=0

echo "== [1/2] Required governance docs present =="
REQUIRED_DOCS=(AGENTS.md CLAUDE.md)
for doc in "${REQUIRED_DOCS[@]}"; do
  if [[ -f "$doc" ]]; then
    echo "  ok: $doc"
  else
    echo "  MISSING: $doc"
    FAILED=1
  fi
done

echo "== [2/2] Markdown link check (root governance docs) =="
if [[ -f local_pr_pipeline/docs_link_check_local.py ]]; then
  "$PYTHON" local_pr_pipeline/docs_link_check_local.py || FAILED=1
else
  echo "  skip: docs_link_check_local.py not present"
fi

echo ""
if [[ $FAILED -eq 0 ]]; then
  echo "✅ Docs consistency checks passed"
else
  echo "❌ Docs consistency checks failed"
fi
exit $FAILED
