#!/usr/bin/env bash
# Evaluate (and optionally merge) a PR: pr-pipeline.yml + agent review comments.
#
# Merge mode loops until CLEAN:
#   1. Wait for pr-pipeline.yml on current HEAD
#   2. Verify PR Pipeline Gate + phase checks
#   3. Verify agent review comments (unresolved threads, blocking audit/AI)
#   4. Verify required GitHub checks
#   If not clean: stop (or watch for new commits when PR_MERGE_WATCH=1)
#   If clean and merge mode: gh pr merge
#
# Requires: gh auth login
#
# Usage:
#   scripts/pr_evaluate_and_merge.sh evaluate [PR_NUMBER]
#   scripts/pr_evaluate_and_merge.sh merge   [PR_NUMBER]
#
# Env:
#   PR                      PR number (else current branch's open PR)
#   PR_WORKFLOW             default: pr-pipeline.yml
#   PR_GATE_CHECK           default: PR Pipeline Gate
#   PR_PHASE_CHECKS         comma-separated phase jobs
#   PR_WAIT_TIMEOUT         per-run wait seconds (default: 7200)
#   PR_MERGE_POLL_TIMEOUT   wait for new commit after blocking (default: 7200)
#   PR_MERGE_MAX_ROUNDS     max evaluate rounds in watch mode (default: 30)
#   PR_MERGE_WATCH          1 = after blocking, wait for push and re-check (merge default: 1)
#   PR_MERGE_DRY_RUN        1 = never merge
#   PR_MERGE_METHOD         squash | merge | rebase
#   PR_RERUN                1 = dispatch workflow before wait
#   PR_SKIP_REVIEW_GATE     1 = skip agent comment gate (not recommended)

set -euo pipefail

MODE="${1:-evaluate}"
PR_ARG="${2:-}"

PR_WORKFLOW="${PR_WORKFLOW:-pr-pipeline.yml}"
PR_GATE_CHECK="${PR_GATE_CHECK:-PR Pipeline Gate}"
PR_PHASE_CHECKS="${PR_PHASE_CHECKS:-validate,lint,semgrep,test,security,compliance,l9,docs}"
PR_WAIT_TIMEOUT="${PR_WAIT_TIMEOUT:-7200}"
PR_MERGE_POLL_TIMEOUT="${PR_MERGE_POLL_TIMEOUT:-7200}"
PR_MERGE_MAX_ROUNDS="${PR_MERGE_MAX_ROUNDS:-30}"
PR_MERGE_DRY_RUN="${PR_MERGE_DRY_RUN:-0}"
PR_MERGE_METHOD="${PR_MERGE_METHOD:-squash}"
PR_DELETE_BRANCH="${PR_DELETE_BRANCH:-0}"
PR_RERUN="${PR_RERUN:-0}"
PR_MERGE_AUTO="${PR_MERGE_AUTO:-0}"
PR_SKIP_REVIEW_GATE="${PR_SKIP_REVIEW_GATE:-0}"

# merge mode: watch for fix pushes by default
if [[ "$MODE" == "merge" ]]; then
  PR_MERGE_WATCH="${PR_MERGE_WATCH:-1}"
else
  PR_MERGE_WATCH="${PR_MERGE_WATCH:-0}"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

info() {
  echo "$*"
}

section() {
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "$*"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

require_gh() {
  command -v gh >/dev/null 2>&1 || die "gh CLI not found. Install: https://cli.github.com/"
  if ! gh auth status -h github.com >/dev/null 2>&1; then
    die "gh not authenticated. Run: gh auth login"
  fi
}

resolve_pr() {
  local n="${PR:-$PR_ARG}"
  if [[ -n "$n" ]]; then
    echo "$n"
    return
  fi
  gh pr view --json number -q .number 2>/dev/null || die "No open PR for current branch. Set PR=<number>."
}

head_sha() {
  gh pr view "$1" --json headRefOid -q .headRefOid
}

branch_name() {
  gh pr view "$1" --json headRefName -q .headRefName
}

run_id_for_head() {
  local branch="$1"
  local sha="$2"
  gh run list \
    --workflow "$PR_WORKFLOW" \
    --branch "$branch" \
    --json databaseId,headSha,status,conclusion,createdAt \
    -q "[.[] | select(.headSha==\"$sha\")][0].databaseId" 2>/dev/null || true
}

dispatch_workflow() {
  local branch="$1"
  info "Dispatching $PR_WORKFLOW on $branch ..."
  gh workflow run "$PR_WORKFLOW" --ref "$branch"
  sleep 10
}

wait_for_workflow_run() {
  local branch="$1"
  local sha="$2"
  local run_id="$3"
  local deadline=$((SECONDS + PR_WAIT_TIMEOUT))

  while [[ -z "$run_id" ]] && (( SECONDS < deadline )); do
    if [[ "$PR_RERUN" == "1" ]]; then
      dispatch_workflow "$branch"
      PR_RERUN=0
    fi
    run_id="$(run_id_for_head "$branch" "$sha")"
    if [[ -z "$run_id" ]]; then
      info "Waiting for $PR_WORKFLOW run on ${sha:0:7} ..."
      sleep 20
    fi
  done

  [[ -n "$run_id" ]] || die "No $PR_WORKFLOW run for commit ${sha:0:7}. Push branch or PR_RERUN=1."

  info "Watching run $run_id for ${sha:0:7} (timeout ${PR_WAIT_TIMEOUT}s) ..."
  while (( SECONDS < deadline )); do
    local status conclusion
    read -r status conclusion < <(
      gh run view "$run_id" --json status,conclusion -q '[.status,.conclusion] | @tsv' 2>/dev/null || echo "unknown	"
    )
    case "$status" in
      completed)
        [[ "$conclusion" == "success" ]] && return 0
        die "Workflow run $run_id failed: $conclusion"
        ;;
      queued | in_progress | pending | waiting | requested)
        sleep 30
        ;;
      *)
        sleep 30
        ;;
    esac
  done
  die "Timed out waiting for run $run_id"
}

verify_pr_pipeline_checks() {
  local pr="$1"
  local data
  data="$(gh pr view "$pr" --json number,title,url,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup)"

  PR_JSON="$data" PR_GATE_CHECK="$PR_GATE_CHECK" PR_PHASE_CHECKS="$PR_PHASE_CHECKS" python3 <<'PY' || return 1
import json
import os
import sys

data = json.loads(os.environ["PR_JSON"])
gate = os.environ["PR_GATE_CHECK"]
phases = [p.strip() for p in os.environ["PR_PHASE_CHECKS"].split(",") if p.strip()]

if data.get("mergeable") == "CONFLICTING":
    print("ERROR: merge conflicts", file=sys.stderr)
    sys.exit(1)
if data.get("mergeStateStatus") == "BEHIND":
    print("ERROR: branch behind base", file=sys.stderr)
    sys.exit(1)

rollup = {c["name"]: c for c in (data.get("statusCheckRollup") or [])}
required = phases + [gate]
failed = []
missing = []

for name in required:
    c = rollup.get(name)
    if not c:
        missing.append(name)
        continue
    state = (c.get("state") or "").upper()
    if state not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
        failed.append(f"{name}={state}")

if failed:
    print("CI FAIL:", ", ".join(failed), file=sys.stderr)
    sys.exit(1)
if missing:
    print("CI WARN: checks missing from rollup:", ", ".join(missing), file=sys.stderr)
    sys.exit(1)
print(f"OK: {gate} + pr-pipeline phases green")
PY
}

verify_required_checks() {
  local pr="$1"
  info "Checking required GitHub status checks ..."
  if gh pr checks "$pr" --required 2>/dev/null; then
    info "OK: all required checks passing"
    return 0
  fi
  info "FAIL: required checks not passing (gh pr checks $pr --required)"
  return 1
}

verify_review_gate() {
  local pr="$1"
  if [[ "$PR_SKIP_REVIEW_GATE" == "1" ]]; then
    info "SKIP: review gate (PR_SKIP_REVIEW_GATE=1)"
    return 0
  fi
  section "Agent / code review comment gate"
  PR="$pr" python3 scripts/pr_review_gate.py || return 1
}

wait_for_new_commit() {
  local pr="$1"
  local old_sha="$2"
  local deadline=$((SECONDS + PR_MERGE_POLL_TIMEOUT))
  info "Waiting for new commit on PR #$pr (timeout ${PR_MERGE_POLL_TIMEOUT}s) ..."
  while (( SECONDS < deadline )); do
    local new_sha
    new_sha="$(head_sha "$pr")"
    if [[ "$new_sha" != "$old_sha" ]]; then
      info "New commit: ${new_sha:0:7}"
      return 0
    fi
    sleep 45
  done
  return 1
}

evaluate_once() {
  local pr="$1"
  local branch sha run_id
  branch="$(branch_name "$pr")"
  sha="$(head_sha "$pr")"

  section "CI: $PR_WORKFLOW @ ${sha:0:7}"
  run_id="$(run_id_for_head "$branch" "$sha")"
  wait_for_workflow_run "$branch" "$sha" "$run_id"
  verify_pr_pipeline_checks "$pr"
  verify_required_checks "$pr"
  verify_review_gate "$pr"
}

do_merge() {
  local pr="$1"
  if [[ "$PR_MERGE_DRY_RUN" == "1" || "$MODE" == "evaluate" ]]; then
    info ""
    info "Clean — merge skipped (evaluate / dry-run)."
    info "To merge: make pr-merge PR=$pr"
    return 0
  fi

  local -a merge_args=(gh pr merge "$pr" "--$PR_MERGE_METHOD")
  [[ "$PR_DELETE_BRANCH" == "1" ]] && merge_args+=(--delete-branch)
  [[ "$PR_MERGE_AUTO" == "1" ]] && merge_args+=(--auto)

  section "Merge PR #$pr"
  "${merge_args[@]}"
  info "Merged PR #$pr"
}

main() {
  case "$MODE" in
    evaluate|merge|check) ;;
    *)
      die "Usage: $0 {evaluate|merge} [PR_NUMBER]"
      ;;
  esac

  require_gh
  local pr
  pr="$(resolve_pr)"

  section "PR merge gate — mode: $MODE — PR #$pr"
  info "Pipeline: $PR_WORKFLOW | Review gate: $([[ $PR_SKIP_REVIEW_GATE == 1 ]] && echo off || echo on) | Watch: $PR_MERGE_WATCH"

  local round=0
  local last_eval_sha=""

  while :; do
    round=$((round + 1))
    [[ "$round" -le "$PR_MERGE_MAX_ROUNDS" ]] || die "Exceeded PR_MERGE_MAX_ROUNDS rounds without CLEAN state"

    local sha
    sha="$(head_sha "$pr")"

    if [[ "$sha" != "$last_eval_sha" ]]; then
      if ! evaluate_once "$pr"; then
        if [[ "$PR_MERGE_WATCH" == "1" ]]; then
          info ""
          info "Not CLEAN. Fix review comments / CI failures, push to branch."
          if wait_for_new_commit "$pr" "$sha"; then
            last_eval_sha=""  # force re-eval on new sha
            continue
          fi
        fi
        die "PR #$pr not CLEAN — resolve issues and re-run make pr-merge"
      fi
      last_eval_sha="$sha"
    fi

    # CLEAN at this point
    section "CLEAN — PR #$pr ready"
    do_merge "$pr"
    return 0
  done
}

main "$@"
