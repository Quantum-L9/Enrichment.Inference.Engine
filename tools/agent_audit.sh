#!/usr/bin/env bash
# --- L9_META ---
# l9_schema: 1
# origin: l9-template
# engine: enrichment
# layer: [ci, governance, diagnostics]
# tags: [L9_TEMPLATE, ci, fail-open, audit, diagnostics]
# owner: platform
# status: active
# --- /L9_META ---
#
# agent_audit.sh — "Fail-open" diagnostic sweep.
#
# Unlike `make agent-check` (which is fail-closed: it stops at the FIRST
# failing gate via `&&`/Make's default abort-on-error), this script runs
# EVERY gate independently, never stops early, and prints a single summary
# table of every gate's pass/fail state at the end. Use this when you want
# to see the *complete* inventory of pre-existing issues in one pass instead
# of discovering them one at a time across many round trips.
#
# This script is purely diagnostic/advisory: it does not fix anything, it
# does not gate merges, and it is NOT wired into branch protection. It exits
# non-zero if any gate failed, so it can still be used in a CI job or as a
# manual triage tool, but its value is the full report, not fail-fast.
#
# Usage:
#   bash tools/agent_audit.sh            # run every gate, print full report
#   bash tools/agent_audit.sh --quiet    # suppress gate stdout/stderr, table only
#
# Local equivalent of the CI "fail_open" workflow_dispatch input on
# PR Pipeline (Full Gate) — see .github/workflows/pr-pipeline.yml.

set -u
# Intentionally NOT `set -e`: every gate must run regardless of earlier failures.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python3}"
QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1

RESULTS=()   # "gate_name|exit_code"
LOG_DIR="$(mktemp -d)"

run_gate() {
  local name="$1"
  shift
  local log_file="$LOG_DIR/${name//[^a-zA-Z0-9_]/_}.log"

  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "▶ ${name}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

  if [[ $QUIET -eq 1 ]]; then
    "$@" >"$log_file" 2>&1
  else
    "$@" 2>&1 | tee "$log_file"
  fi
  local status=${PIPESTATUS[0]:-$?}

  RESULTS+=("${name}|${status}|${log_file}")
  return 0
}

# Skip a gate cleanly (e.g. optional tool not installed) without breaking the report.
skip_gate() {
  local name="$1" reason="$2"
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "⏭  ${name} (SKIPPED: ${reason})"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  RESULTS+=("${name}|SKIP|-")
}

# ── Gate 1: Ruff lint ─────────────────────────────────────────────────────
run_gate "ruff-check" ruff check .

# ── Gate 2: Ruff format ───────────────────────────────────────────────────
run_gate "ruff-format-check" ruff format --check .

# ── Gate 3: Mypy types (non-blocking upstream per WAIVER-001, still reported) ─
run_gate "mypy" mypy app --show-error-codes --ignore-missing-imports

# ── Gate 4: Unit + compliance tests ───────────────────────────────────────
run_gate "pytest-unit-compliance" "$PYTHON" -m pytest tests/unit/ tests/compliance/ -q --tb=short

# ── Gate 5: CI contract-enforcement tests ─────────────────────────────────
run_gate "pytest-ci" "$PYTHON" -m pytest tests/ci/ -q --tb=short -o addopts=""

# ── Gate 6: 27-rule audit engine ──────────────────────────────────────────
run_gate "audit-engine" env PYTHONPATH=. "$PYTHON" tools/audit_engine.py --strict

# ── Gate 7: Contract manifest verification ────────────────────────────────
run_gate "verify-contracts" env PYTHONPATH=. "$PYTHON" tools/verify_contracts.py

# ── Gate 8: Chassis isolation (FastAPI import boundary) ───────────────────
run_gate "chassis-isolation" bash -c '
  violations=$(find app/ -name "*.py" \
    -not -path "app/api/*" \
    -not -path "app/middleware/*" \
    -not -path "app/main.py" \
    -not -path "app/core/auth.py" \
    -not -path "app/score/score_api.py" \
    -not -name "handlers.py" \
    -exec grep -l "from fastapi import\|import fastapi" {} + 2>/dev/null || true)
  if [ -n "$violations" ]; then
    echo "FastAPI imports found outside allowed modules:"
    echo "$violations"
    exit 1
  fi
  echo "Chassis isolation is maintained"
'

# ── Gate 9: KB YAML schema validation ─────────────────────────────────────
if [[ -f local_pr_pipeline/compliance_kb_validate.py ]]; then
  run_gate "kb-yaml-validate" "$PYTHON" local_pr_pipeline/compliance_kb_validate.py
else
  skip_gate "kb-yaml-validate" "local_pr_pipeline/compliance_kb_validate.py not found"
fi

# ── Gate 10: L9 node constitution ─────────────────────────────────────────
if [[ -f scripts/verify_node_constitution.py ]]; then
  run_gate "l9-node-constitution" env PYTHONPATH=. "$PYTHON" scripts/verify_node_constitution.py
else
  skip_gate "l9-node-constitution" "scripts/verify_node_constitution.py not found"
fi

# ── Gate 11/12: L9 contract control (constitution + attestation) ─────────
if [[ -f scripts/l9_contract_control.py ]]; then
  run_gate "l9-contract-control-constitution" env PYTHONPATH=. "$PYTHON" scripts/l9_contract_control.py verify-constitution
  run_gate "l9-contract-control-attestation" env PYTHONPATH=. "$PYTHON" scripts/l9_contract_control.py verify-attestation
else
  skip_gate "l9-contract-control-constitution" "scripts/l9_contract_control.py not found"
  skip_gate "l9-contract-control-attestation" "scripts/l9_contract_control.py not found"
fi

# ── Gate 13: Bandit SAST (optional tool) ──────────────────────────────────
if command -v bandit >/dev/null 2>&1; then
  run_gate "bandit" bandit -r app -ll -f screen --exclude ./venv,./.venv,./tests,./build,./dist
else
  skip_gate "bandit" "bandit not installed"
fi

# ── Gate 14: Semgrep policy check (optional tool) ─────────────────────────
if command -v semgrep >/dev/null 2>&1 && [[ -d .semgrep ]]; then
  run_gate "semgrep" semgrep --config .semgrep/ --error --quiet
else
  skip_gate "semgrep" "semgrep not installed or .semgrep/ missing"
fi

# ── Summary ────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  FAIL-OPEN AUDIT SUMMARY — every gate ran independently       ║"
echo "╚══════════════════════════════════════════════════════════════╝"
printf "%-38s %-10s %s\n" "GATE" "RESULT" "LOG"
printf "%-38s %-10s %s\n" "----" "------" "---"

FAILED=0
for entry in "${RESULTS[@]}"; do
  IFS='|' read -r name code log <<<"$entry"
  if [[ "$code" == "SKIP" ]]; then
    printf "%-38s %-10s %s\n" "$name" "SKIP" "-"
  elif [[ "$code" == "0" ]]; then
    printf "%-38s %-10s %s\n" "$name" "PASS" "$log"
  else
    printf "%-38s %-10s %s\n" "$name" "FAIL" "$log"
    FAILED=1
  fi
done

echo ""
if [[ $FAILED -eq 1 ]]; then
  echo "Result: ISSUES FOUND — see per-gate logs above/in $LOG_DIR for full detail."
  echo "This is expected for a diagnostic run; use the report to plan remediation."
  exit 1
fi

echo "Result: ALL GATES PASSED"
exit 0
