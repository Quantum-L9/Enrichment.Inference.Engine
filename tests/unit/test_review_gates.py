"""WU-23: review gates fail closed and follow the review verdict.

F-031: an unparseable AI reply blocks.
F-032: a blocking audit comment, in the format audit-pr-review.yml writes, blocks.
F-033: the gate follows the review's verdict, not the workflow's `blocking` input.
"""

from __future__ import annotations

import subprocess

import pytest

from scripts import pr_review_gate
from tools.ai_review import parse_review, print_results, review_verdict

_AI = pr_review_gate.MARKER_AI_REVIEW
_AUDIT = pr_review_gate.MARKER_L9_AUDIT


# ── F-031 ─────────────────────────────────────────────────────────────


def test_free_text_reply_blocks() -> None:
    result = parse_review("Critical: SQL injection in app/services/foo.py line 12")
    assert print_results(result) is True
    assert review_verdict(result) == "block"


def test_fenced_json_reply_is_parsed() -> None:
    result = parse_review('```json\n{"issues": [], "summary": "ok", "block": false}\n```')
    assert review_verdict(result) == "pass"
    assert print_results(result) is False


def test_invalid_embedded_json_blocks_instead_of_raising() -> None:
    result = parse_review("Here you go: {issues: [oops}")
    assert review_verdict(result) == "block"


def test_non_object_json_blocks() -> None:
    assert review_verdict(parse_review('["not", "a", "review"]')) == "block"


# ── F-033 verdict policy ──────────────────────────────────────────────


def test_critical_issue_blocks_even_when_model_says_block_false() -> None:
    result = {"issues": [{"severity": "critical", "description": "x"}], "block": False}
    assert review_verdict(result) == "block"


def test_medium_only_review_passes() -> None:
    result = {"issues": [{"severity": "medium", "description": "x"}], "block": False}
    assert review_verdict(result) == "pass"


def test_verdict_line_is_printed(capsys: pytest.CaptureFixture[str]) -> None:
    print_results({"issues": [{"severity": "high", "description": "x"}], "block": False})
    assert "L9-AI-REVIEW-VERDICT: block" in capsys.readouterr().out


def _ai_comment(verdict: str | None, blocking_input: str) -> str:
    output = "⚠️  AI Review: 1 issue(s) found\n  🔴 [CRITICAL] SQL injection\n"
    if verdict:
        output += f"L9-AI-REVIEW-VERDICT: {verdict}\n"
    return (
        f"{_AI}\n## 🤖 AI Code Review (Perplexity)\n\n**PR:** #7\n"
        f"**Blocking:** {blocking_input}\n\n---\n\n```\n{output}```\n"
    )


def test_gate_follows_block_verdict_when_blocking_input_is_false() -> None:
    assert pr_review_gate._ai_review_blocking(_ai_comment("block", "false")) is True


def test_gate_follows_pass_verdict_when_blocking_input_is_true() -> None:
    assert pr_review_gate._ai_review_blocking(_ai_comment("pass", "true")) is False


def test_ai_comment_without_a_verdict_blocks() -> None:
    assert pr_review_gate._ai_review_blocking(_ai_comment(None, "false")) is True


def test_non_review_comment_is_ignored() -> None:
    assert pr_review_gate._ai_review_blocking("LGTM") is False


# ── F-032 ─────────────────────────────────────────────────────────────


def _audit_comment(blocking: int) -> str:
    """Exactly the status block audit-pr-review.yml builds."""
    lines = [
        _AUDIT,
        "## L9 Audit Review",
        "",
        "### Status",
        f"- Result: `{'blocking' if blocking else 'pass_with_advisories'}`",
        "- Mode: `explicit_advisory_blocking_tier_policy`",
        "- Total findings visible: **5**",
        f"- Blocking findings: **{blocking}**",
        f"- Advisory findings: **{5 - blocking}**",
    ]
    return "\n".join(lines)


def test_blocking_audit_comment_as_generated_is_detected() -> None:
    assert pr_review_gate._audit_comment_blocking(_audit_comment(2)) is True


def test_advisory_only_audit_comment_does_not_block() -> None:
    assert pr_review_gate._audit_comment_blocking(_audit_comment(0)) is False


def test_workflow_still_writes_the_parsed_audit_format() -> None:
    """The gate's parser and the workflow's writer must not drift apart again."""
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parents[2] / ".github" / "workflows" / "audit-pr-review.yml"
    ).read_text()
    assert "- Result: `{'blocking' if payload['blocking_finding_count']" in workflow
    assert "- Blocking findings: **{payload['blocking_finding_count']}**" in workflow
    assert _AUDIT in workflow


def test_unfetchable_review_threads_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_review_gate, "_pr_data", lambda pr: {"reviews": []})
    monkeypatch.setattr(pr_review_gate, "_issue_comments", lambda pr: [])

    def boom(pr: int) -> list:
        raise subprocess.CalledProcessError(1, ["gh", "api", "graphql"])

    monkeypatch.setattr(pr_review_gate, "_review_threads", boom)
    blocking, _ = pr_review_gate.analyze(7)
    assert any("review threads" in b for b in blocking)
