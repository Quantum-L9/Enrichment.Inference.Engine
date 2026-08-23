"""
tests/test_pr20_import_fix.py

Proves PR#20 import path fix is live:
  - rank_fields_by_unlock and RuleRegistry are importable from
    app.engines.inference.rule_loader (corrected path)
  - meta_prompt_planner.py does not contain any reference to the
    old stale path "inference_rule_loader"
  - inference_unlock_scorer.py does not reference the stale path
"""

from __future__ import annotations

import ast
import pathlib


def test_rule_loader_import_resolves():
    """PR#20: rank_fields_by_unlock must be importable from the corrected path."""
    from app.engines.inference.rule_loader import RuleRegistry, rank_fields_by_unlock  # noqa: F401

    assert callable(rank_fields_by_unlock)
    assert RuleRegistry is not None


def test_rank_fields_by_unlock_returns_list():
    """rank_fields_by_unlock must return a list given a registry and missing fields."""
    from app.engines.inference.rule_loader import RuleRegistry, rank_fields_by_unlock

    registry = RuleRegistry()
    result = rank_fields_by_unlock(
        missing_fields=[
            {"field_name": "material_type", "is_gate_critical": False, "scoring_weight": 0.85}
        ],
        unlock_index={},
        registry=registry,
        domain_spec=None,
    )
    assert isinstance(result, list)


def _find_stale_import(src: str, stale: str) -> list[int]:
    """Return line numbers where stale import name appears in AST import nodes."""
    tree = ast.parse(src)
    bad_lines = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raw = ast.dump(node)
            if stale in raw:
                bad_lines.append(node.lineno)
    return bad_lines


def test_meta_prompt_planner_no_stale_import():
    """PR#20: meta_prompt_planner.py must not reference 'inference_rule_loader'."""
    path = pathlib.Path("app/engines/meta_prompt_planner.py")
    assert path.exists(), "meta_prompt_planner.py must exist for the PR#20 import-fix check"
    lines = _find_stale_import(path.read_text(), "inference_rule_loader")
    assert lines == [], (
        f"Stale import 'inference_rule_loader' found in meta_prompt_planner.py at lines: {lines}"
    )


def test_inference_unlock_scorer_no_stale_import():
    """PR#20: inference_unlock_scorer.py must not reference 'inference_rule_loader'."""
    path = pathlib.Path("app/engines/inference_unlock_scorer.py")
    assert path.exists(), "inference_unlock_scorer.py must exist for the PR#20 import-fix check"
    lines = _find_stale_import(path.read_text(), "inference_rule_loader")
    assert lines == [], (
        f"Stale import 'inference_rule_loader' found in inference_unlock_scorer.py "
        f"at lines: {lines}"
    )


def test_no_module_level_import_of_stale_path_in_pkg():
    """Broad scan: no Python file in app/engines/ may import inference_rule_loader."""
    engines_dir = pathlib.Path("app/engines")
    assert engines_dir.exists(), "app/engines/ must exist for the PR#20 import-fix check"

    offenders: list[str] = []
    for py_file in engines_dir.rglob("*.py"):
        try:
            src = py_file.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if "inference_rule_loader" in src:
            offenders.append(str(py_file))

    assert offenders == [], f"Files still reference stale 'inference_rule_loader': {offenders}"
