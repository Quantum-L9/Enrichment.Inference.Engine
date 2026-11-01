#!/usr/bin/env python3
"""AI code review script with L9 contract awareness.

Usage:
    # Review staged changes (pre-commit)
    python tools/ai_review.py --mode staged

    # Review a diff file (CI)
    python tools/ai_review.py --mode file --diff-path pr_diff.txt

    # Review specific files
    python tools/ai_review.py --mode files --paths app/main.py app/services/foo.py

    # Skip contract context (faster, generic review)
    python tools/ai_review.py --mode staged --no-context

Requires PERPLEXITY_API_KEY in .env or environment.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import httpx

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"

# Context files to include (in priority order, truncated to fit token budget)
# These are the repo rules that code review agents need to know
CONTEXT_FILES = [
    ("AGENTS.md", 12000),  # Agent rules, tiers, prohibited actions, directory ownership
    ("ARCHITECTURE.md", 8000),  # System architecture, components, data flow
    ("CLAUDE.md", 4000),  # AI coding context, contracts summary
]

BASE_SYSTEM_PROMPT = """You are a senior Python engineer performing a code review for an L9 constellation engine.

## Review Focus

1. **Contract Violations**: Check against the L9 contracts provided in context
2. **Security**: injection, auth bypass, SSRF, path traversal, unvalidated input
3. **Architecture**: Chassis boundary violations, direct HTTP calls between nodes, missing TransportPacket / Gate routing
4. **Bugs**: logic errors, None dereference, missing awaits on async calls
5. **Concurrency**: race conditions, deadlocks, unbounded task fan-out
6. **Error handling**: bare except, swallowed exceptions

## L9-Specific Rules (from context)

- Engine NEVER imports FastAPI, Starlette, or HTTP libraries
- Engine NEVER creates routes — only action handlers
- Inter-node communication uses TransportPacket via Gate/SDK and chassis wire envelopes where applicable
- Cypher queries MUST use sanitize_label() for labels/types
- No eval(), exec(), pickle.load(), yaml.load() without SafeLoader
- No hardcoded secrets
- structlog only (no print() in engine code)

## Output Format

Output STRICT JSON (no markdown fences):
{
  "issues": [
    {
      "severity": "critical|high|medium",
      "category": "contract|security|architecture|bug|concurrency|error_handling",
      "file": "filename",
      "line_hint": "code snippet or line ref",
      "description": "what is wrong",
      "contract_ref": "Contract N or AGENTS.md rule if applicable",
      "suggestion": "how to fix"
    }
  ],
  "summary": "1-2 sentence overall assessment",
  "block": true/false
}

Rules:
- Do NOT flag style, formatting, or naming
- Do NOT flag missing docstrings
- ONLY flag issues with real impact
- Set block=true only for critical/high severity issues
- If no issues: {"issues": [], "summary": "No issues found.", "block": false}
"""


def get_api_key() -> str:
    """Get Perplexity API key from app config or environment."""
    import os

    try:
        from app.core.config import get_settings

        settings = get_settings()
        if settings.perplexity_api_key:
            return settings.perplexity_api_key
    except ImportError:
        pass

    key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not key:
        print("ERROR: PERPLEXITY_API_KEY not set in .env or environment", file=sys.stderr)
        sys.exit(1)
    return key


def get_model() -> str:
    """Get Perplexity model from app config or default."""
    try:
        from app.core.config import get_settings

        settings = get_settings()
        if settings.perplexity_model:
            return settings.perplexity_model
    except ImportError:
        pass
    return "sonar-pro"


def load_context_files() -> str:
    """Load repo context files for the review prompt."""
    context_parts = []

    for filename, max_chars in CONTEXT_FILES:
        filepath = PROJECT_ROOT / filename
        if filepath.exists():
            try:
                content = filepath.read_text()[:max_chars]
                if len(content) == max_chars:
                    content += "\n... [truncated]"
                context_parts.append(f"## {filename}\n\n{content}")
            except Exception as e:
                print(f"Warning: Could not read {filename}: {e}", file=sys.stderr)

    if not context_parts:
        return ""

    return "# REPOSITORY CONTEXT\n\n" + "\n\n---\n\n".join(context_parts)


def build_system_prompt(include_context: bool) -> str:
    """Build the system prompt, optionally with repo context."""
    if not include_context:
        # Fallback to generic security-focused prompt
        return """You are a senior Python engineer performing a security-focused code review.
Analyze for: bugs, security issues, concurrency problems, error handling, performance.
Do NOT flag style/formatting. ONLY flag issues with real impact.

Output STRICT JSON (no markdown fences):
{
  "issues": [{"severity": "critical|high|medium", "file": "...", "line_hint": "...", "description": "...", "suggestion": "..."}],
  "summary": "1-2 sentence assessment",
  "block": true/false
}
If no issues: {"issues": [], "summary": "No issues found.", "block": false}
"""

    context = load_context_files()
    if context:
        return f"{context}\n\n---\n\n{BASE_SYSTEM_PROMPT}"
    return BASE_SYSTEM_PROMPT


def call_review(code: str, api_key: str, model: str, system_prompt: str) -> dict:
    """Send code to Perplexity for review."""
    # Truncate code if needed (leave room for system prompt)
    max_code_chars = 80_000
    if len(code) > max_code_chars:
        code = code[:max_code_chars] + "\n\n... [code truncated]"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Review this code change:\n\n```\n{code}\n```"},
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
    }

    with httpx.Client(timeout=180) as client:
        resp = client.post(
            PERPLEXITY_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()

    return parse_review(resp.json()["choices"][0]["message"]["content"])


#: Severities that block regardless of the model's own ``block`` flag (F-033).
BLOCKING_SEVERITIES = frozenset({"critical", "high"})

#: Machine-readable verdict line; scripts/pr_review_gate.py parses it from the
#: posted review comment.
VERDICT_PREFIX = "L9-AI-REVIEW-VERDICT:"


def parse_review(content: str) -> dict:
    """Parse a model reply into the review dict.

    F-031: a reply that is not the requested JSON object was not reviewed, so it
    fails closed (``block=True``) instead of reading as "no issues found".
    """
    import re

    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    candidates = [content]
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match:
        candidates.append(match.group())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("issues", []), list):
            return parsed

    return {
        "issues": [],
        "summary": f"Unparseable review reply (fail-closed): {content[:200]}",
        "block": True,
        "parse_error": True,
    }


def review_verdict(result: dict) -> str:
    """``block`` when the model asked to block, the reply was unparseable, or
    any issue is critical/high — whatever ``--no-block`` or the workflow's
    ``blocking`` input say about exit codes (F-033)."""
    if result.get("block") or result.get("parse_error"):
        return "block"
    for issue in result.get("issues") or []:
        if str(issue.get("severity", "")).lower() in BLOCKING_SEVERITIES:
            return "block"
    return "pass"


def get_staged_diff() -> str:
    """Get git staged changes."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    return result.stdout


def get_file_contents(paths: list[str]) -> str:
    """Read contents of specified files."""
    chunks = []
    for p in paths:
        path = Path(p) if Path(p).is_absolute() else PROJECT_ROOT / p
        if path.exists() and path.stat().st_size < 80_000:
            chunks.append(f"# FILE: {p}\n{path.read_text()}")
    return "\n\n".join(chunks)


def print_results(result: dict) -> bool:
    """Print findings, return True if should block."""
    issues = result.get("issues", [])
    summary = result.get("summary", "")
    block = review_verdict(result) == "block"

    if not issues:
        if block:
            print("\n❌ AI Review: no usable review — blocking (fail-closed).")
            print(f"   {summary}")
        else:
            print("\n✅ AI Review: No issues found.")
            print(f"   {summary}")
        print(f"{VERDICT_PREFIX} {'block' if block else 'pass'}")
        return block

    print(f"\n⚠️  AI Review: {len(issues)} issue(s) found")
    print(f"   {summary}\n")

    for i, issue in enumerate(issues, 1):
        sev = issue.get("severity", "?").upper()
        cat = issue.get("category", "")
        icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡"}.get(sev, "⚪")

        desc = issue.get("description", "")
        contract_ref = issue.get("contract_ref", "")

        print(f"  {icon} [{sev}] {desc}")
        if cat:
            print(f"     Category: {cat}")
        if issue.get("file"):
            print(f"     File: {issue['file']}")
        if issue.get("line_hint"):
            print(f"     Near: {issue['line_hint']}")
        if contract_ref:
            print(f"     Contract: {contract_ref}")
        if issue.get("suggestion"):
            print(f"     Fix: {issue['suggestion']}")
        print()

    if block:
        print("❌ BLOCKING — critical/high severity issues must be fixed.\n")
    print(f"{VERDICT_PREFIX} {'block' if block else 'pass'}")
    return block


def main():
    parser = argparse.ArgumentParser(
        description="AI Code Review with L9 Contract Awareness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/ai_review.py --mode staged
  python tools/ai_review.py --mode file --diff-path pr.diff
  python tools/ai_review.py --mode files --paths app/main.py app/services/foo.py
  python tools/ai_review.py --mode staged --no-context  # Skip contract context
        """,
    )
    parser.add_argument(
        "--mode",
        choices=["staged", "file", "files"],
        default="staged",
        help="Review mode: staged git changes, diff file, or specific files",
    )
    parser.add_argument("--diff-path", help="Path to diff file (mode=file)")
    parser.add_argument("--paths", nargs="*", help="File paths to review (mode=files)")
    parser.add_argument("--model", default=None, help="Perplexity model (default: from config)")
    parser.add_argument("--no-block", action="store_true", help="Never exit non-zero")
    parser.add_argument(
        "--no-context",
        action="store_true",
        help="Skip loading repo context (faster, generic review)",
    )
    args = parser.parse_args()

    api_key = get_api_key()
    model = args.model or get_model()

    # Get code to review
    if args.mode == "staged":
        code = get_staged_diff()
        if not code.strip():
            print("No staged changes to review.")
            sys.exit(0)
    elif args.mode == "file":
        if not args.diff_path:
            print("ERROR: --diff-path required for mode=file", file=sys.stderr)
            sys.exit(1)
        diff_path = Path(args.diff_path)
        if not diff_path.exists():
            print(f"ERROR: Diff file not found: {args.diff_path}", file=sys.stderr)
            sys.exit(1)
        code = diff_path.read_text()
    elif args.mode == "files":
        if not args.paths:
            print("ERROR: --paths required for mode=files", file=sys.stderr)
            sys.exit(1)
        code = get_file_contents(args.paths)
        if not code.strip():
            print("ERROR: No valid files found to review", file=sys.stderr)
            sys.exit(1)
    else:
        sys.exit(1)

    # Build prompt with or without context
    include_context = not args.no_context
    system_prompt = build_system_prompt(include_context)

    context_status = "with L9 contracts" if include_context else "generic"
    print(f"🔍 Running AI review ({model}, {context_status})...")

    result = call_review(code, api_key, model, system_prompt)
    should_block = print_results(result)

    if should_block and not args.no_block:
        sys.exit(1)


if __name__ == "__main__":
    main()
