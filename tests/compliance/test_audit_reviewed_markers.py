"""Every `# l9-audit-reviewed:` marker must carry a real justification.

The audit engine has no suppression mechanism by design, and adding one is a
risk: a silencer that costs nothing gets used to silence. Three properties
keep this honest, and this file enforces the first two.

1. The reason is mandatory and length-checked. A bare marker suppresses
   nothing -- `reviewed_reason` returns None and the finding stays live.
2. A marker must actually sit against a line the rule flags. A stale marker
   left behind after the code moved is dead weight that implies review which
   no longer applies.
3. Acknowledged findings are still reported, in their own section of the
   audit output, and counted in the summary. That one is a property of
   `audit_engine.main` rather than something assertable here.

Same shape as test_staged_artifacts_have_reasons in
tests/compliance/test_module_reachability.py: declare it, and say why.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
APP_DIR = REPO_ROOT / "app"

_MARKER = re.compile(r"#\s*l9-audit-reviewed:\s*rule(?P<rule>\d+)\s*(?:--|—)?\s*(?P<reason>.*)$")


def _marker_lines() -> list[tuple[Path, int, str, str]]:
    """(path, 1-based line, rule, reason) for every marker under app/."""
    found: list[tuple[Path, int, str, str]] = []
    for py in sorted(APP_DIR.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            m = _MARKER.search(line)
            if m:
                found.append((py, i, m.group("rule"), m.group("reason").strip()))
    return found


def test_every_marker_has_a_substantive_reason() -> None:
    from tools.audit_engine import REVIEWED_MIN_REASON

    thin = [
        f"{p.relative_to(REPO_ROOT)}:{ln} rule{rule} -> {reason!r}"
        for p, ln, rule, reason in _marker_lines()
        if len(reason) < REVIEWED_MIN_REASON
    ]
    assert thin == [], (
        "l9-audit-reviewed markers without a substantive reason:\n  "
        + "\n  ".join(thin)
        + f"\n\nA marker suppresses nothing unless it explains why the finding is "
        f"acceptable, in at least {REVIEWED_MIN_REASON} characters."
    )


def test_every_marker_still_suppresses_something() -> None:
    """A marker that acknowledges nothing is stale and must be removed.

    This runs the audit and cross-references real findings. An earlier version
    asked `reviewed_reason` whether the marker parsed, which is not the same
    question: a marker naming a rule that never fires on that line parses
    fine and suppresses nothing, so the check passed while claiming to catch
    exactly that case.
    """
    from tools.audit_engine import GROUPS, AuditResult, get_py_files

    result = AuditResult()
    files = get_py_files()
    for fn in GROUPS.values():
        fn(files, result)

    # (rule, file, line) for everything the audit actually flagged.
    fired = {(f.rule, f.file, f.line) for f in result.findings}

    stale: list[str] = []
    for path, line_no, rule, _reason in _marker_lines():
        rel = str(path.relative_to(REPO_ROOT))
        # The marker sits on the flagged line or the line immediately above.
        if not any((int(rule), rel, ln) in fired for ln in (line_no, line_no + 1)):
            stale.append(f"{rel}:{line_no} rule{rule}")

    assert stale == [], (
        "l9-audit-reviewed markers that acknowledge no finding:\n  "
        + "\n  ".join(stale)
        + "\n\nThe rule named does not fire on that line. Remove the marker, or "
        "move it back onto the line it was written for."
    )
