#!/usr/bin/env python3
"""L9 Audit Engine — 27-Rule Scanner (Enrichment.Inference.Engine).

Scans engine code for contract violations across 5 groups:
  NAMING (1-5), SECURITY (6-10), IMPORTS (11-15),
  ERROR (16-19), COMPLETENESS (20-23), PATTERNS (24-27)

Adapted from L9 golden template for the enrichment engine (app/ source dir).

Usage:
  python tools/audit_engine.py              # Run all rules
  python tools/audit_engine.py --strict     # Exit 1 on CRITICAL/HIGH
  python tools/audit_engine.py --group naming
  python tools/audit_engine.py --fix        # Auto-fix where possible
  python tools/audit_engine.py --json       # JSON output
  python tools/audit_engine.py --exclude app/legacy/
"""

from __future__ import annotations

import argparse
import ast
import json as json_mod
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
ENGINE_DIR = REPO_ROOT / "app"


#: Statement verbs that make an interpolated query a mutation rather than a
#: read. SOQL has none of them -- it is SELECT-only.
_SQL_WRITE_KEYWORDS = frozenset({"INSERT", "UPDATE", "DELETE", "DROP"})

_FSTRING_PREFIX_RE = re.compile(r"""f["']""")
_SQL_KEYWORD_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|DROP)\s", re.IGNORECASE)


def sql_keywords_in_fstring(line: str) -> set[str]:
    """Return every SQL statement verb appearing inside an f-string on `line`.

    Returns an empty set when the line has no f-string, or an f-string with no
    statement verb. Scanning from the f-string prefix rather than capturing a
    single keyword is what lets the caller see ALL the verbs present, so a
    mixed statement is classified by what it does rather than by whichever
    keyword a greedy `.*` reached last.
    """
    m = _FSTRING_PREFIX_RE.search(line)
    if not m:
        return set()
    return {kw.upper() for kw in _SQL_KEYWORD_RE.findall(line[m.start() :])}


def sql_finding_severity(keywords: set[str]) -> str:
    """Grade an interpolated query by what its verbs can do.

    A mutation is CRITICAL; a read is HIGH. Kept next to
    sql_keywords_in_fstring, and used by both rule 10 and its tests, so the
    rule and its regression cases cannot drift apart on the definition.
    """
    return "CRITICAL" if keywords & _SQL_WRITE_KEYWORDS else "HIGH"


#: An in-tree acknowledgement of a finding the tool cannot reason about.
#: Form: `# l9-audit-reviewed: rule<N> -- <reason>`
#: The reason is mandatory and length-checked; a bare marker suppresses
#: nothing. Acknowledged findings are still reported, in their own section.
REVIEWED_MARKER_RE = re.compile(
    # Greedy `.+$` rather than a lazy `.+?` paired with `\s*$`: the reason is
    # stripped in Python anyway, so the two are equivalent, and the greedy form
    # has no ambiguity for the engine to backtrack over.
    r"#\s*l9-audit-reviewed:\s*rule(?P<rule>\d+)\s*(?:--|—)\s*(?P<reason>.+)$"
)

#: A reason shorter than this is not a justification.
REVIEWED_MIN_REASON = 30


def reviewed_reason(lines: list[str], index: int, rule: int) -> str | None:
    """Return the justification acknowledging `rule` at `lines[index]`, if any.

    The marker may sit on the flagged line or on the line immediately above
    it. Allowing the line above keeps long statements readable -- a trailing
    marker on an already-long query line would push it past any sane width --
    and matches how a reader writes the annotation by hand.

    Mirrors the declare-with-a-reason pattern the test suite already uses for
    STAGED_ARTIFACTS and UNREACHABLE_BASELINE: an exemption must say why, and
    the reason is enforced rather than advisory.
    """
    candidates = [lines[index]]
    if index > 0:
        candidates.append(lines[index - 1])
    for line in candidates:
        m = REVIEWED_MARKER_RE.search(line)
        if not m or int(m.group("rule")) != rule:
            continue
        reason = m.group("reason").strip()
        if len(reason) >= REVIEWED_MIN_REASON:
            return reason
    return None


@dataclass
class Finding:
    severity: str  # CRITICAL, HIGH, MEDIUM, INFO
    code: str  # C-001, H-007, etc.
    rule: int
    group: str
    message: str
    file: str
    line: int
    fix_hint: str | None = None
    #: Non-empty when a `# l9-audit-reviewed:` marker acknowledged this
    #: finding. Reviewed findings are still reported and still counted --
    #: they are reported separately and excluded from the --strict exit,
    #: never dropped.
    reviewed: str | None = None


@dataclass
class AuditResult:
    findings: list = field(default_factory=list)

    def add(self, **kwargs):
        self.findings.append(Finding(**kwargs))

    @property
    def live(self) -> list:
        """Findings that have not been acknowledged by a reviewed marker."""
        return [f for f in self.findings if not f.reviewed]

    @property
    def reviewed(self) -> list:
        """Findings acknowledged in-tree with a justification."""
        return [f for f in self.findings if f.reviewed]

    @property
    def critical_count(self):
        return sum(1 for f in self.live if f.severity == "CRITICAL")

    @property
    def high_count(self):
        return sum(1 for f in self.live if f.severity == "HIGH")

    @property
    def medium_count(self):
        return sum(1 for f in self.live if f.severity == "MEDIUM")

    @property
    def info_count(self):
        return sum(1 for f in self.live if f.severity == "INFO")


def get_py_files(exclude: list[str] | None = None) -> list[Path]:
    if not ENGINE_DIR.exists():
        return []
    exclude = exclude or []
    files = []
    for f in ENGINE_DIR.rglob("*.py"):
        if not any(ex in str(f) for ex in exclude):
            files.append(f)
    return files


# ============================================================
# GROUP: NAMING (Rules 1-5)
# ============================================================
def check_naming(files: list[Path], result: AuditResult):
    counter = 0
    for py_file in files:
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        name = item.target.id
                        rel = py_file.relative_to(REPO_ROOT)
                        # Rule 1: camelCase detection
                        if re.match(r"^[a-z]+[A-Z]", name):
                            counter += 1
                            result.add(
                                severity="CRITICAL",
                                code=f"C-{counter:03d}",
                                rule=1,
                                group="naming",
                                message=f"camelCase field '{name}'",
                                file=str(rel),
                                line=item.lineno,
                                fix_hint="Rename to " + re.sub(r"([A-Z])", r"_\1", name).lower(),
                            )
                        # Rule 2: flatcase detection (long single-word fields)
                        elif len(name) > 12 and "_" not in name and name.islower():
                            counter += 1
                            result.add(
                                severity="CRITICAL",
                                code=f"C-{counter:03d}",
                                rule=2,
                                group="naming",
                                message=f"Likely flatcase field '{name}'",
                                file=str(rel),
                                line=item.lineno,
                                fix_hint="Add underscores: e.g., candidate_prop not candidateprop",
                            )
                        # Rule 3: Field(alias=...) detection
                        if isinstance(item.value, ast.Call):
                            for kw in getattr(item.value, "keywords", []):
                                if kw.arg == "alias":
                                    counter += 1
                                    result.add(
                                        severity="CRITICAL",
                                        code=f"C-{counter:03d}",
                                        rule=3,
                                        group="naming",
                                        message=f"Field alias on '{name}' -- banned per FIELD_NAMES.md",
                                        file=str(rel),
                                        line=item.lineno,
                                        fix_hint="Remove alias. YAML key = Python field = attribute access.",
                                    )
                # Rule 4: populate_by_name in model_config
                for item in node.body:
                    if isinstance(item, ast.Assign):
                        for target in item.targets:
                            if isinstance(target, ast.Name) and target.id == "model_config":
                                src = ast.get_source_segment(py_file.read_text(), item) or ""
                                if "populate_by_name" in src:
                                    counter += 1
                                    result.add(
                                        severity="CRITICAL",
                                        code=f"C-{counter:03d}",
                                        rule=4,
                                        group="naming",
                                        message="populate_by_name in model_config -- banned",
                                        file=str(py_file.relative_to(REPO_ROOT)),
                                        line=item.lineno,
                                        fix_hint="Remove populate_by_name. No aliases = no need.",
                                    )
    # Rule 5: YAML key naming (check kb/ rule files)
    kb_dir = REPO_ROOT / "kb"
    if kb_dir.exists():
        import yaml

        for yaml_file in kb_dir.rglob("*.yaml"):
            try:
                data = yaml.safe_load(yaml_file.read_text())
            except Exception:
                continue
            if isinstance(data, dict):
                _check_yaml_keys(data, yaml_file, result, counter)


def _check_yaml_keys(data: dict, yaml_file: Path, result: AuditResult, counter: int):
    """Recursively check YAML keys for camelCase."""
    for key in data:
        if isinstance(key, str) and re.match(r"^[a-z]+[A-Z]", key):
            counter += 1
            result.add(
                severity="HIGH",
                code=f"H-{counter:03d}",
                rule=5,
                group="naming",
                message=f"camelCase YAML key '{key}' in {yaml_file.relative_to(REPO_ROOT)}",
                file=str(yaml_file.relative_to(REPO_ROOT)),
                line=0,
                fix_hint="Rename to " + re.sub(r"([A-Z])", r"_\1", key).lower(),
            )
        if isinstance(data[key], dict):
            _check_yaml_keys(data[key], yaml_file, result, counter)


# ============================================================
# GROUP: SECURITY (Rules 6-10)
# ============================================================
def check_security(files: list[Path], result: AuditResult):
    counter = 100
    for py_file in files:
        try:
            source = py_file.read_text()
            tree = ast.parse(source)
        except SyntaxError:
            continue
        rel = py_file.relative_to(REPO_ROOT)
        lines = source.split("\n")

        for node in ast.walk(tree):
            # Rule 6: eval/exec/compile
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in ("eval", "exec", "compile"):
                    counter += 1
                    result.add(
                        severity="CRITICAL",
                        code=f"C-{counter:03d}",
                        rule=6,
                        group="security",
                        message=f"{node.func.id}() call -- use dispatch table instead",
                        file=str(rel),
                        line=node.lineno,
                        fix_hint="Replace with explicit dispatch: OPERATORS[op](a, b)",
                    )

        # Rule 7: Hardcoded API keys (enrichment-specific)
        api_key_patterns = [
            r'(?:api_key|apikey|secret|token|password)\s*=\s*["\'][^"\']{8,}["\']',
            r"pplx-[a-zA-Z0-9]{20,}",
            r"sk-[a-zA-Z0-9]{20,}",
            r"Bearer\s+[a-zA-Z0-9\-._~+/]+=*",
        ]
        for i, line in enumerate(lines, 1):
            for pattern in api_key_patterns:
                if re.search(pattern, line, re.IGNORECASE):
                    # Skip if it's in a comment about patterns or test data
                    stripped = line.strip()
                    if stripped.startswith("#") or "example" in stripped.lower():
                        continue
                    counter += 1
                    result.add(
                        severity="CRITICAL",
                        code=f"C-{counter:03d}",
                        rule=7,
                        group="security",
                        message="Potential hardcoded API key/secret",
                        file=str(rel),
                        line=i,
                        fix_hint="Use environment variables or settings injection",
                    )

        # Rule 8: subprocess with shell=True
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr in (
                    "run",
                    "Popen",
                    "call",
                ):
                    for kw in node.keywords:
                        if (
                            kw.arg == "shell"
                            and isinstance(kw.value, ast.Constant)
                            and kw.value.value is True
                        ):
                            counter += 1
                            result.add(
                                severity="HIGH",
                                code=f"H-{counter:03d}",
                                rule=8,
                                group="security",
                                message="subprocess with shell=True -- injection risk",
                                file=str(rel),
                                line=node.lineno,
                                fix_hint="Use shell=False with list args",
                            )

        # Rule 9: pickle usage
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "pickle":
                        counter += 1
                        result.add(
                            severity="HIGH",
                            code=f"H-{counter:03d}",
                            rule=9,
                            group="security",
                            message="pickle import -- deserialization risk",
                            file=str(rel),
                            line=node.lineno,
                            fix_hint="Use json or msgpack instead",
                        )
            if isinstance(node, ast.ImportFrom) and node.module == "pickle":
                counter += 1
                result.add(
                    severity="HIGH",
                    code=f"H-{counter:03d}",
                    rule=9,
                    group="security",
                    message="pickle import -- deserialization risk",
                    file=str(rel),
                    line=node.lineno,
                    fix_hint="Use json or msgpack instead",
                )

        # Rule 10: SQL injection patterns (raw string formatting in queries)
        #
        # Severity is split by what the statement can DO. This is a line
        # regex with no taint model, so it cannot tell a guarded query from
        # an unguarded one -- but it can tell a read from a write. A SELECT
        # is a disclosure risk; INSERT/UPDATE/DELETE/DROP are mutation. They
        # were previously graded identically, which mattered because
        # --strict exits on CRITICAL *or* HIGH, leaving severity as the only
        # signal a reader gets. It also mis-grades SOQL, which is
        # SELECT-only and has no DML at all.
        for i, line in enumerate(lines, 1):
            keywords = sql_keywords_in_fstring(line)
            if not keywords:
                continue
            # Classify by the statement's effective operation, not by whichever
            # keyword a regex happened to capture last. `INSERT INTO t SELECT
            # ...` and `DELETE ... WHERE id IN (SELECT ...)` both mutate, and a
            # greedy match would capture the trailing SELECT and grade them as
            # reads -- downgrading exactly the queries that matter most.
            severity = sql_finding_severity(keywords)
            prefix = "C" if severity == "CRITICAL" else "H"
            counter += 1
            result.add(
                severity=severity,
                code=f"{prefix}-{counter:03d}",
                rule=10,
                group="security",
                message="f-string SQL query -- injection risk",
                file=str(rel),
                line=i,
                fix_hint="Use parameterized queries",
                reviewed=reviewed_reason(lines, i - 1, 10),
            )


# ============================================================
# GROUP: IMPORTS (Rules 11-15)
# ============================================================
def check_imports(files: list[Path], result: AuditResult):
    counter = 200
    for py_file in files:
        try:
            source = py_file.read_text()
            tree = ast.parse(source)
        except SyntaxError:
            continue
        rel = py_file.relative_to(REPO_ROOT)

        # Rule 11: FastAPI imports outside api/ and main.py (chassis isolation)
        is_api = "api/" in str(rel) or str(rel).endswith("main.py") or "handlers.py" in str(rel)
        if not is_api:
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith("fastapi"):
                        counter += 1
                        result.add(
                            severity="CRITICAL",
                            code=f"C-{counter:03d}",
                            rule=11,
                            group="imports",
                            message="FastAPI import in engine module -- chassis isolation violation",
                            file=str(rel),
                            line=node.lineno,
                            fix_hint="Engine modules must be framework-agnostic. Move to api/ or handlers.",
                        )

        # Rule 12: Phantom imports (internal modules that don't resolve)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith(("app.", "app/")):
                    mod_path = REPO_ROOT / node.module.replace(".", "/")
                    if not (
                        mod_path.with_suffix(".py").exists() or (mod_path / "__init__.py").exists()
                    ):
                        counter += 1
                        result.add(
                            severity="HIGH",
                            code=f"H-{counter:03d}",
                            rule=12,
                            group="imports",
                            message=f"Unresolved import '{node.module}'",
                            file=str(rel),
                            line=node.lineno,
                            fix_hint="Verify module path exists or fix the import",
                        )

        # Rule 13: Circular import risk (engine importing from api)
        is_engine = any(
            d in str(rel) for d in ["engines/", "score/", "health/", "models/", "services/"]
        )
        if is_engine:
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if "api" in node.module.split(".") and "api" not in str(rel):
                        counter += 1
                        result.add(
                            severity="HIGH",
                            code=f"H-{counter:03d}",
                            rule=13,
                            group="imports",
                            message=f"Engine module imports from api layer: {node.module}",
                            file=str(rel),
                            line=node.lineno,
                            fix_hint="Invert the dependency. API imports engine, not vice versa.",
                        )

        # Rule 14: Star imports
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.names:
                for alias in node.names:
                    if alias.name == "*":
                        counter += 1
                        result.add(
                            severity="MEDIUM",
                            code=f"M-{counter:03d}",
                            rule=14,
                            group="imports",
                            message=f"Star import from {node.module}",
                            file=str(rel),
                            line=node.lineno,
                            fix_hint="Import specific names explicitly",
                        )

        # Rule 15: Relative imports deeper than 2 levels
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level and node.level > 2:
                counter += 1
                result.add(
                    severity="MEDIUM",
                    code=f"M-{counter:03d}",
                    rule=15,
                    group="imports",
                    message=f"Deep relative import (level={node.level})",
                    file=str(rel),
                    line=node.lineno,
                    fix_hint="Use absolute imports from app.*",
                )


# ============================================================
# GROUP: ERROR (Rules 16-19)
# ============================================================
def check_error(files: list[Path], result: AuditResult):
    counter = 300
    for py_file in files:
        try:
            source = py_file.read_text()
            tree = ast.parse(source)
        except SyntaxError:
            continue
        rel = py_file.relative_to(REPO_ROOT)

        for node in ast.walk(tree):
            # Rule 16: bare except
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                counter += 1
                result.add(
                    severity="HIGH",
                    code=f"H-{counter:03d}",
                    rule=16,
                    group="error",
                    message="Bare except: -- catches SystemExit, KeyboardInterrupt",
                    file=str(rel),
                    line=node.lineno,
                    fix_hint="Use except Exception: or a specific type",
                )

            # Rule 17: except Exception with pass (silent swallow)
            if isinstance(node, ast.ExceptHandler):
                if node.type and isinstance(node.type, ast.Name) and node.type.id == "Exception":
                    if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                        counter += 1
                        result.add(
                            severity="HIGH",
                            code=f"H-{counter:03d}",
                            rule=17,
                            group="error",
                            message="except Exception: pass -- silent error swallowing",
                            file=str(rel),
                            line=node.lineno,
                            fix_hint="Log the error or re-raise",
                        )

            # Rule 18: raise without from in except blocks
            if isinstance(node, ast.ExceptHandler):
                for child in ast.walk(node):
                    if isinstance(child, ast.Raise) and child.exc and not child.cause:
                        # Only flag if raising a new exception (not re-raising)
                        if isinstance(child.exc, ast.Call):
                            counter += 1
                            result.add(
                                severity="MEDIUM",
                                code=f"M-{counter:03d}",
                                rule=18,
                                group="error",
                                message="raise NewException() without 'from' -- loses traceback",
                                file=str(rel),
                                line=child.lineno,
                                fix_hint="Use 'raise NewException(...) from e'",
                            )

        # Rule 19: print() instead of logger
        lines = source.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("print(") and not stripped.startswith("#"):
                counter += 1
                result.add(
                    severity="MEDIUM",
                    code=f"M-{counter:03d}",
                    rule=19,
                    group="error",
                    message="print() call -- use structlog logger instead",
                    file=str(rel),
                    line=i,
                    fix_hint="Replace with logger.info/debug/warning/error",
                )


# ============================================================
# GROUP: COMPLETENESS (Rules 20-23)
# ============================================================
def check_completeness(files: list[Path], result: AuditResult):
    counter = 400
    # Rule 20: Every module must have a docstring
    for py_file in files:
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue
        rel = py_file.relative_to(REPO_ROOT)
        if str(rel).endswith("__init__.py"):
            continue
        docstring = ast.get_docstring(tree)
        if not docstring:
            counter += 1
            result.add(
                severity="MEDIUM",
                code=f"M-{counter:03d}",
                rule=20,
                group="completeness",
                message="Module missing docstring",
                file=str(rel),
                line=1,
                fix_hint="Add a module-level docstring describing purpose",
            )

    # Rule 21: Every public class must have a docstring
    for py_file in files:
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue
        rel = py_file.relative_to(REPO_ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                docstring = ast.get_docstring(node)
                if not docstring:
                    counter += 1
                    result.add(
                        severity="MEDIUM",
                        code=f"M-{counter:03d}",
                        rule=21,
                        group="completeness",
                        message=f"Public class '{node.name}' missing docstring",
                        file=str(rel),
                        line=node.lineno,
                        fix_hint="Add a class docstring",
                    )

    # Rule 22: Every public function/method must have a docstring
    for py_file in files:
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue
        rel = py_file.relative_to(REPO_ROOT)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    docstring = ast.get_docstring(node)
                    if not docstring:
                        counter += 1
                        result.add(
                            severity="INFO",
                            code=f"I-{counter:03d}",
                            rule=22,
                            group="completeness",
                            message=f"Public function '{node.name}' missing docstring",
                            file=str(rel),
                            line=node.lineno,
                            fix_hint="Add a function docstring",
                        )

    # Rule 23: Every test file must have at least one test function
    test_dir = REPO_ROOT / "tests"
    if test_dir.exists():
        for test_file in test_dir.rglob("test_*.py"):
            try:
                tree = ast.parse(test_file.read_text())
            except SyntaxError:
                continue
            has_test = False
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name.startswith("test_"):
                        has_test = True
                        break
            if not has_test:
                counter += 1
                result.add(
                    severity="HIGH",
                    code=f"H-{counter:03d}",
                    rule=23,
                    group="completeness",
                    message="Test file has no test functions",
                    file=str(test_file.relative_to(REPO_ROOT)),
                    line=1,
                    fix_hint="Add at least one test_* function",
                )


# ============================================================
# GROUP: PATTERNS (Rules 24-27)
# ============================================================
def check_patterns(files: list[Path], result: AuditResult):
    counter = 500
    for py_file in files:
        try:
            source = py_file.read_text()
            tree = ast.parse(source)
        except SyntaxError:
            continue
        rel = py_file.relative_to(REPO_ROOT)
        lines = source.split("\n")

        # Rule 24: TODO/FIXME/HACK without ticket reference
        for i, line in enumerate(lines, 1):
            for marker in ("TODO", "FIXME", "HACK", "XXX"):
                if marker in line and "#" in line:
                    # Check if there's a ticket reference after the marker
                    after_marker = line[line.index(marker) + len(marker) :]
                    if not re.search(r"[A-Z]+-\d+|#\d+|GH-\d+", after_marker):
                        counter += 1
                        result.add(
                            severity="INFO",
                            code=f"I-{counter:03d}",
                            rule=24,
                            group="patterns",
                            message=f"{marker} without ticket reference",
                            file=str(rel),
                            line=i,
                            fix_hint=f"Add ticket: # {marker}(PROJ-123): description",
                        )

        # Rule 25: Magic numbers (numeric literals > 1 outside of constants/configs)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                if abs(node.value) > 1 and node.value not in (2, 10, 100, 1000):
                    # Check if it's in an assignment to an UPPER_CASE name
                    # This is a heuristic -- we flag but at INFO level
                    pass  # Too noisy for now, keep as placeholder

        # Rule 26: Functions longer than 50 lines
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end_line = getattr(node, "end_lineno", None)
                if end_line:
                    length = end_line - node.lineno
                    if length > 50:
                        counter += 1
                        result.add(
                            severity="MEDIUM",
                            code=f"M-{counter:03d}",
                            rule=26,
                            group="patterns",
                            message=f"Function '{node.name}' is {length} lines (max 50)",
                            file=str(rel),
                            line=node.lineno,
                            fix_hint="Extract helper functions to reduce complexity",
                        )

        # Rule 27: Deeply nested code (> 4 levels of indentation in logic)
        for i, line in enumerate(lines, 1):
            if line.strip() and not line.strip().startswith("#"):
                indent = len(line) - len(line.lstrip())
                # 4 spaces per level, > 4 levels = > 16 spaces
                if indent >= 20:  # 5+ levels
                    counter += 1
                    result.add(
                        severity="INFO",
                        code=f"I-{counter:03d}",
                        rule=27,
                        group="patterns",
                        message="Deeply nested code (5+ levels)",
                        file=str(rel),
                        line=i,
                        fix_hint="Extract to helper function or use early returns",
                    )


# ============================================================
# MAIN
# ============================================================
GROUPS = {
    "naming": check_naming,
    "security": check_security,
    "imports": check_imports,
    "error": check_error,
    "completeness": check_completeness,
    "patterns": check_patterns,
}


def main():
    parser = argparse.ArgumentParser(description="L9 Audit Engine — 27-Rule Scanner")
    parser.add_argument("--strict", action="store_true", help="Exit 1 on CRITICAL/HIGH")
    parser.add_argument("--group", choices=list(GROUPS.keys()), help="Run single group")
    parser.add_argument("--fix", action="store_true", help="Auto-fix where possible")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--exclude", nargs="*", default=[], help="Exclude paths")
    args = parser.parse_args()

    files = get_py_files(exclude=args.exclude)
    result = AuditResult()

    if args.group:
        GROUPS[args.group](files, result)
    else:
        for fn in GROUPS.values():
            fn(files, result)

    if args.json:
        output = [
            {
                "severity": f.severity,
                "code": f.code,
                "rule": f.rule,
                "group": f.group,
                "message": f.message,
                "file": f.file,
                "line": f.line,
                "fix_hint": f.fix_hint,
                "reviewed": f.reviewed,
            }
            for f in result.findings
        ]
        print(json_mod.dumps(output, indent=2))
    else:
        if not result.findings:
            print("No findings. All 27 rules pass.")
        for f in sorted(result.live, key=lambda x: x.severity):
            sev = f"{f.severity:8s}"
            print(f"{sev} [{f.code}] {f.message}")
            print(f"         {f.file}:{f.line}")
            if f.fix_hint:
                print(f"         Fix: {f.fix_hint}")
            print()

        # Acknowledged findings are shown, not hidden. A reader must be able
        # to see what was waived and on what grounds without reading source.
        if result.reviewed:
            print(f"Reviewed (acknowledged in-tree) -- {len(result.reviewed)}:")
            for f in sorted(result.reviewed, key=lambda x: (x.file, x.line)):
                print(f"         [{f.code}] {f.message}")
                print(f"         {f.file}:{f.line}")
                print(f"         Reason: {f.reviewed}")
                print()

        print(
            f"Summary: {result.critical_count} CRITICAL, {result.high_count} HIGH, "
            f"{result.medium_count} MEDIUM, {result.info_count} INFO"
            + (f", {len(result.reviewed)} REVIEWED" if result.reviewed else "")
        )

    if args.strict and (result.critical_count > 0 or result.high_count > 0):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
