#!/usr/bin/env bash
# --- L9_META ---
# l9_schema: 1
# origin: l9-implementer
# engine: enrichment
# layer: [ci, docs]
# tags: [docs-consistency, pr-pipeline]
# owner: platform
# status: active
# --- /L9_META ---
#
# Local wrapper mirroring .github/workflows/docs-consistency.yml checks.
# Invoked by pr-pipeline.yml Docs Consistency job.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

echo "==> Docs consistency (local_pr_pipeline mirror of docs-consistency.yml)"

if [[ -f "AGENT.md" ]]; then
  if ! grep -q "Agent Autonomy Tiers" AGENT.md; then
    echo "FAIL: AGENT.md missing 'Agent Autonomy Tiers' section"
    exit 1
  fi
  echo "PASS: AGENT.md contains Autonomy Tiers"
else
  echo "SKIP: AGENT.md not found"
fi

COUNT=$(grep -rl "make agent-check" ./*.md 2>/dev/null | wc -l || echo "0")
COUNT=$(echo "${COUNT}" | tr -d ' ')
if [[ "${COUNT}" -gt 15 ]]; then
  echo "FAIL: 'make agent-check' appears in ${COUNT} markdown files (max 15 allowed)"
  grep -rl "make agent-check" ./*.md || true
  exit 1
fi
echo "PASS: make agent-check referenced in ${COUNT} files (within limit)"

if [[ -f "AGENT.md" ]]; then
  if ! grep -q "GUARDRAILS.md" AGENT.md; then
    echo "FAIL: AGENT.md does not reference GUARDRAILS.md"
    exit 1
  fi
  echo "PASS: AGENT.md references GUARDRAILS.md"
fi

if [[ -f "INVARIANTS.md" ]]; then
  for i in $(seq 1 20); do
    if ! grep -q "INV-${i}" INVARIANTS.md; then
      echo "FAIL: INVARIANTS.md missing INV-${i}"
      exit 1
    fi
  done
  echo "PASS: All INV-1 through INV-20 present in INVARIANTS.md"
else
  echo "SKIP: INVARIANTS.md not found"
fi

if [[ -f "CI_WHITELIST_REGISTER.md" ]]; then
  for i in $(seq 1 7); do
    if ! grep -q "ADR-00${i}" CI_WHITELIST_REGISTER.md; then
      echo "FAIL: CI_WHITELIST_REGISTER.md missing ADR-00${i} reference for WAIVER-00${i}"
      exit 1
    fi
  done
  echo "PASS: All 7 WAIVER->ADR references present"
else
  echo "SKIP: CI_WHITELIST_REGISTER.md not found"
fi

if [[ -d "docs/adr" ]]; then
  for i in $(seq 1 7); do
    NUM=$(printf "%03d" "${i}")
    if ! ls docs/adr/ADR-${NUM}-*.md 1>/dev/null 2>&1; then
      echo "FAIL: docs/adr/ADR-${NUM}-*.md not found"
      exit 1
    fi
  done
  echo "PASS: All 7 ADR files exist"
else
  echo "SKIP: docs/adr/ directory not found"
fi

if [[ -f "AGENT_BOOTSTRAP.md" ]]; then
  if ! grep -q "AGENT.md" AGENT_BOOTSTRAP.md; then
    echo "FAIL: AGENT_BOOTSTRAP.md does not reference AGENT.md"
    exit 1
  fi
  echo "PASS: AGENT_BOOTSTRAP.md present and references AGENT.md"
else
  echo "SKIP: AGENT_BOOTSTRAP.md not found"
fi

echo "✅ Docs consistency checks passed"
