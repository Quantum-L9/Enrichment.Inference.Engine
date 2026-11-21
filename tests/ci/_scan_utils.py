"""Shared CI scanner utilities for L9 Enrichment Engine test suite.

Provides:
- get_repo_root()        — walk up from __file__ to find .git or pyproject.toml
- iter_python_files()    — glob-aware file collection (include/exclude) for contract scanner
- get_python_files()     — simple list-returning variant for anti-pattern tests
- parse_python_file()    — AST parse with graceful error handling
"""

from __future__ import annotations

import ast
import logging
import pathlib
from fnmatch import fnmatch
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

# ── Repo Root Resolution ────────────────────────────────────────


def get_repo_root() -> pathlib.Path:
    """Return the repository root by walking up from this file.

    Looks for ``.git/`` directory first, then ``pyproject.toml`` as a
    fallback sentinel.

    Raises
    ------
    RuntimeError
        If neither marker is found in any ancestor directory.
    """
    current = pathlib.Path(__file__).resolve()
    for parent in current.parents:
        if (parent / ".git").is_dir():
            return parent
        if (parent / "pyproject.toml").is_file():
            return parent
    msg = "Could not determine repo root from _scan_utils.py location"
    raise RuntimeError(msg)


# ── Glob-Based File Collection (Contract Scanner) ──────────────


def _matches_any_glob(rel_path: str, globs: list[str]) -> bool:
    """Check if a relative path matches any of the given glob patterns."""
    parts = pathlib.PurePosixPath(rel_path).parts
    for glob in globs:
        if fnmatch(rel_path, glob):
            return True
        for i in range(len(parts)):
            sub = str(pathlib.PurePosixPath(*parts[: i + 1]))
            if fnmatch(sub, glob) or fnmatch(sub + "/", glob):
                return True
    return False


def iter_python_files(
    repo_root: pathlib.Path,
    include_globs: list[str],
    exclude_globs: list[str],
) -> list[pathlib.Path]:
    """Collect Python files under *repo_root* matching include/exclude globs.

    Returns a deterministically sorted list of absolute ``Path`` objects.
    """
    collected: list[pathlib.Path] = []
    for candidate in sorted(repo_root.rglob("*.py")):
        rel = str(candidate.relative_to(repo_root))
        is_included = any(fnmatch(rel, g) for g in include_globs)
        if not is_included:
            continue
        if _matches_any_glob(rel, exclude_globs):
            continue
        collected.append(candidate)

    logger.debug("python_files_collected count=%d repo_root=%s", len(collected), repo_root)
    return collected


# ── Simple File Collection ───────────────────────────────────────

SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        "venv",
        ".venv",
        ".pytest_cache",
        "build",
        "dist",
        ".egg-info",
        ".mypy_cache",
        ".ruff_cache",
        "htmlcov",
        # Peer repositories ci.yml checks out beside this one (CEG, Gate).
        ".ci-subjects",
    }
)


def _iter_python_files_simple(
    root: pathlib.Path | None = None,
    *,
    skip_dirs: frozenset[str] | None = None,
) -> Iterator[pathlib.Path]:
    """Yield all Python files under *root*, skipping excluded dirs."""
    if root is None:
        root = get_repo_root()
    effective_skip = skip_dirs if skip_dirs is not None else SKIP_DIRS

    for py_file in sorted(root.rglob("*.py")):
        path_str = str(py_file)
        if any(skip in path_str for skip in effective_skip):
            continue
        yield py_file


def get_python_files(
    root: pathlib.Path | None = None,
    *,
    skip_dirs: frozenset[str] | None = None,
) -> list[pathlib.Path]:
    """Return a sorted list of all Python files under *root*."""
    return list(_iter_python_files_simple(root, skip_dirs=skip_dirs))


# ── AST Parsing ────────────────────────────────────────────────


def parse_python_file(path: pathlib.Path) -> ast.Module | None:
    """Parse a Python file into an AST, returning ``None`` on errors."""
    try:
        source = path.read_text(encoding="utf-8")
        return ast.parse(source, filename=str(path))
    except SyntaxError:
        logger.warning("ast_parse_syntax_error file=%s", path)
        return None
    except (OSError, UnicodeDecodeError):
        logger.warning("ast_parse_read_error file=%s", path)
        return None
