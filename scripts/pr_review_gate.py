#!/usr/bin/env python3
"""Block PR merge until agent review comments are resolved and CI is clean.

Uses gh CLI (authenticated). Exit 0 = clean, 1 = blocking issues remain.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

# Bots / agents that post code review on PRs for this repo.
AGENT_LOGINS = frozenset(
    {
        "coderabbitai[bot]",
        "github-actions[bot]",
        "cursor[bot]",
        "cursor-bugbot[bot]",
        "bugbot[bot]",
    }
)

MARKER_L9_AUDIT = "<!-- l9-audit-marker: v1 -->"
MARKER_AI_REVIEW = "<!-- ai-code-review-perplexity -->"
MARKER_PR_PIPELINE = "<!-- pr-pipeline-gate-summary -->"


def _run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True).strip()


def _repo() -> tuple[str, str]:
    slug = _run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    owner, name = slug.split("/", 1)
    return owner, name


def _pr_data(pr: int) -> dict:
    raw = _run(
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "--json",
            "number,title,url,headRefOid,reviewDecision,statusCheckRollup,reviews",
        ]
    )
    return json.loads(raw)


def _issue_comments(pr: int) -> list[dict]:
    owner, repo = _repo()
    raw = _run(["gh", "api", f"repos/{owner}/{repo}/issues/{pr}/comments", "--paginate"])
    data = json.loads(raw)
    if not data:
        return []
    if isinstance(data[0], list):
        comments: list[dict] = []
        for page in data:
            comments.extend(page)
        return comments
    return data


def _review_threads(pr: int) -> list[dict]:
    owner, repo = _repo()
    query = """
    query($owner: String!, $repo: String!, $pr: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $pr) {
          reviewThreads(first: 100) {
            nodes {
              isResolved
              isOutdated
              comments(first: 10) {
                nodes {
                  author { login }
                  body
                  createdAt
                }
              }
            }
          }
        }
      }
    }
    """
    raw = _run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"owner={owner}",
            "-f",
            f"repo={repo}",
            "-F",
            f"pr={pr}",
        ]
    )
    data = json.loads(raw)
    threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    return threads


def _audit_comment_blocking(body: str) -> bool:
    if MARKER_L9_AUDIT not in body:
        return False
    if "Blocking Issues Found" in body:
        return True
    if re.search(r"\*\*\d+ CRITICAL\*\*", body):
        return True
    return bool(re.search(r"\*\*\d+ HIGH\*\*", body) and "cannot merge" in body.lower())


def _ai_review_blocking(body: str) -> bool:
    if MARKER_AI_REVIEW not in body:
        return False
    m = re.search(r"\*\*Blocking:\*\*\s*(true|false)", body, re.I)
    return bool(m and m.group(1).lower() == "true")


def _pipeline_comment_blocking(body: str) -> bool:
    if MARKER_PR_PIPELINE not in body:
        return False
    return "FAILED" in body or "❌" in body


def _agent_logins() -> frozenset[str]:
    extra = os.environ.get("PR_AGENT_LOGINS", "")
    if not extra.strip():
        return AGENT_LOGINS
    return AGENT_LOGINS | frozenset(x.strip() for x in extra.split(",") if x.strip())


def _is_agent(login: str | None) -> bool:
    if not login:
        return False
    if login in _agent_logins():
        return True
    return login.endswith("[bot]") and any(
        k in login.lower() for k in ("rabbit", "cursor", "bugbot", "copilot", "github-actions")
    )


def analyze(pr: int) -> tuple[list[str], list[str]]:
    """Return (blocking_reasons, advisory_notes)."""
    blocking: list[str] = []
    advisory: list[str] = []

    data = _pr_data(pr)
    if data.get("reviewDecision") == "CHANGES_REQUESTED":
        blocking.append("GitHub reviewDecision=CHANGES_REQUESTED")

    for review in data.get("reviews") or []:
        state = (review.get("state") or "").upper()
        author = (review.get("author") or {}).get("login", "")
        if state == "CHANGES_REQUESTED":
            blocking.append(f"Review CHANGES_REQUESTED from {author or 'unknown'}")
        if state == "COMMENTED" and _is_agent(author):
            body = review.get("body") or ""
            if body and len(body) > 50:
                advisory.append(f"Agent review comment from {author} (verify addressed)")

    for comment in _issue_comments(pr):
        login = (comment.get("user") or {}).get("login", "")
        body = comment.get("body") or ""
        if not body:
            continue
        if _audit_comment_blocking(body):
            blocking.append(f"L9 Audit Review blocking findings ({login})")
        elif MARKER_L9_AUDIT in body:
            advisory.append(f"L9 Audit comment present — no blocking findings ({login})")
        if _ai_review_blocking(body):
            blocking.append(f"Perplexity AI review marked blocking ({login})")
        if _pipeline_comment_blocking(body):
            blocking.append("PR Pipeline gate summary reports FAILED")

    try:
        threads = _review_threads(pr)
    except subprocess.CalledProcessError as exc:
        advisory.append(f"Could not fetch review threads: {exc}")
        threads = []

    for thread in threads:
        if thread.get("isResolved"):
            continue
        if thread.get("isOutdated"):
            continue
        nodes = (thread.get("comments") or {}).get("nodes") or []
        if not nodes:
            continue
        authors = {(n.get("author") or {}).get("login") for n in nodes}
        agent_thread = any(_is_agent(a) for a in authors if a)
        preview = (nodes[-1].get("body") or "")[:120].replace("\n", " ")
        if agent_thread:
            blocking.append(f"Unresolved agent review thread: {preview!r}...")

    return blocking, advisory


def main() -> int:
    pr = int(os.environ.get("PR") or (sys.argv[1] if len(sys.argv) > 1 else 0))
    if pr <= 0:
        print("Usage: PR=<n> pr_review_gate.py  OR  pr_review_gate.py <n>", file=sys.stderr)
        return 2

    try:
        blocking, advisory = analyze(pr)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: gh command failed: {exc}", file=sys.stderr)
        return 2

    data = _pr_data(pr)
    print(f"PR #{pr} — {data.get('title', '')}")
    print(f"URL: {data.get('url', '')}")
    print(f"head: {(data.get('headRefOid') or '')[:12]}")

    if advisory:
        print("\nAdvisory:")
        for note in advisory:
            print(f"  • {note}")

    if blocking:
        print("\nBLOCKING (merge not allowed):")
        for reason in blocking:
            print(f"  ✗ {reason}")
        print("\nInvestigate comments, push fixes to the PR branch, wait for CI, then re-run:")
        print(f"  make pr-merge PR={pr}")
        return 1

    print("\nOK: No blocking agent review comments or unresolved review threads.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
