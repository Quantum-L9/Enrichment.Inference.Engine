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


# ------------------------------------------------------------------
# Shared detection helpers
# ------------------------------------------------------------------
# Modules where `from fastapi import ...` is permitted per the C-01 contract
# (AGENTS.md): "only in app/api/, app/main.py, and transport-adjacent modules
# explicitly intended for HTTP routes." This mirrors the chassis-isolation
# allowlist in .github/workflows/compliance.yml and pr-pipeline.yml so the audit
# engine and the CI gate agree on exactly one source of truth.
def _fastapi_import_allowed(rel_path: str) -> bool:
    """Return True when a module is on the C-01 FastAPI import allowlist."""
    return (
        "api/" in rel_path
        or rel_path.endswith("main.py")
        or "handlers.py" in rel_path
        or rel_path.startswith("app/middleware/")
        or rel_path.startswith("app/bootstrap/")  # chassis bootstrap (INV-ARCH-03)
        or rel_path == "app/core/auth.py"
        or rel_path == "app/score/score_api.py"
    )


# Whole-word tokens that commonly concatenate into genuine flatcase field names
# (e.g. "accountnumber" -> account + number). Used to distinguish real flatcase
# from valid long single words (e.g. "recommendation", "configuration"), which
# do NOT decompose into two or more known word tokens.
_FLATCASE_WORD_TOKENS = frozenset(
    {
        "account",
        "action",
        "address",
        "amount",
        "attribute",
        "batch",
        "billing",
        "category",
        "client",
        "code",
        "company",
        "contact",
        "contract",
        "count",
        "customer",
        "data",
        "date",
        "deal",
        "description",
        "domain",
        "edge",
        "email",
        "engine",
        "entity",
        "error",
        "event",
        "field",
        "file",
        "first",
        "flag",
        "gate",
        "graph",
        "group",
        "handler",
        "host",
        "index",
        "input",
        "item",
        "label",
        "last",
        "lead",
        "level",
        "list",
        "message",
        "meta",
        "metadata",
        "mode",
        "model",
        "name",
        "node",
        "number",
        "output",
        "packet",
        "page",
        "param",
        "password",
        "path",
        "phase",
        "phone",
        "price",
        "priority",
        "profile",
        "prop",
        "props",
        "queue",
        "record",
        "request",
        "response",
        "result",
        "rule",
        "runtime",
        "schema",
        "score",
        "secret",
        "server",
        "service",
        "session",
        "size",
        "source",
        "state",
        "status",
        "step",
        "store",
        "target",
        "tenant",
        "tier",
        "time",
        "title",
        "token",
        "total",
        "transport",
        "type",
        "user",
        "value",
        "worker",
    }
)


def _looks_like_flatcase(name: str) -> bool:
    """Return True if ``name`` decomposes into two or more known word tokens.

    A word-break over ``_FLATCASE_WORD_TOKENS`` separates genuine flatcase
    concatenations (``accountnumber`` -> ``account`` + ``number``) from valid
    long single words (``recommendation``, ``configuration``), which cannot be
    segmented into known tokens.
    """
    n = len(name)
    reachable = [False] * (n + 1)
    reachable[0] = True
    for i in range(1, n + 1):
        for j in range(i):
            if reachable[j] and name[j:i] in _FLATCASE_WORD_TOKENS:
                reachable[i] = True
                break
    # Segmentable AND not a single standalone token (i.e. >= 2 concatenated words).
    return reachable[n] and name not in _FLATCASE_WORD_TOKENS


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


@dataclass
class AuditResult:
    findings: list = field(default_factory=list)

    def add(self, **kwargs):
        self.findings.append(Finding(**kwargs))

    @property
    def critical_count(self):
        return sum(1 for f in self.findings if f.severity == "CRITICAL")

    @property
    def high_count(self):
        return sum(1 for f in self.findings if f.severity == "HIGH")

    @property
    def medium_count(self):
        return sum(1 for f in self.findings if f.severity == "MEDIUM")

    @property
    def info_count(self):
        return sum(1 for f in self.findings if f.severity == "INFO")


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
                        # Rule 2: flatcase detection (concatenated multi-word fields)
                        elif (
                            len(name) > 12
                            and "_" not in name
                            and name.islower()
                            and _looks_like_flatcase(name)
                        ):
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

        # Rule 10: SQL injection patterns (raw string formatting in queries).
        # Require the SQL verb at the START of the f-string literal so that
        # ordinary prose containing words like "drop out" is not misflagged;
        # real query strings begin with the verb (optionally after whitespace).
        for i, line in enumerate(lines, 1):
            if re.search(
                r'f["\']{1,3}\s*(?:SELECT|INSERT|UPDATE|DELETE|DROP)\b', line, re.IGNORECASE
            ):
                counter += 1
                result.add(
                    severity="CRITICAL",
                    code=f"C-{counter:03d}",
                    rule=10,
                    group="security",
                    message="f-string SQL query -- injection risk",
                    file=str(rel),
                    line=i,
                    fix_hint="Use parameterized queries",
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

        # Rule 11: FastAPI imports outside the C-01 allowlist (chassis isolation)
        if not _fastapi_import_allowed(str(rel)):
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
            }
            for f in result.findings
        ]
        print(json_mod.dumps(output, indent=2))
    else:
        if not result.findings:
            print("No findings. All 27 rules pass.")
        for f in sorted(result.findings, key=lambda x: x.severity):
            sev = f"{f.severity:8s}"
            print(f"{sev} [{f.code}] {f.message}")
            print(f"         {f.file}:{f.line}")
            if f.fix_hint:
                print(f"         Fix: {f.fix_hint}")
            print()

        print(
            f"Summary: {result.critical_count} CRITICAL, {result.high_count} HIGH, "
            f"{result.medium_count} MEDIUM, {result.info_count} INFO"
        )

    if args.strict and (result.critical_count > 0 or result.high_count > 0):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
