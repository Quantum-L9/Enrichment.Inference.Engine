"""Native release must not gain a Semantic Compiler prerequisite.

L9_META:
  tier: 2
  domain: native-domain
  authority: native-v3 V14 user execution contract 2026-11-03
  pr_class: app_code + tier2_test
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_FORBIDDEN = (
    "semantic_compiler",
    "semantic-compiler",
    "targetir",
    "semanticspec",
    "compilationreceipt",
)


def _assert_native_dependencies(dependencies: list[str]) -> None:
    for dependency in dependencies:
        assert not any(name in dependency.lower() for name in _FORBIDDEN), dependency


@pytest.mark.unit
def test_actual_native_operator_imports_have_no_compiler_edge():
    dependencies = []
    for relative in (
        "scripts/check_domain_compatibility.py",
        "scripts/domain_spec_operator.py",
        "app/services/domain_spec_workflow.py",
        "app/engines/domain_yaml_reader.py",
        "app/engines/domain_spec_schema.py",
        "app/engines/domain_spec_compatibility.py",
    ):
        tree = ast.parse((ROOT / relative).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                dependencies.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                dependencies.append(node.module)
    _assert_native_dependencies(dependencies)
    requirements = (ROOT / "pyproject.toml").read_text().lower()
    assert "semantic-compiler" not in requirements
    assert "semantic_compiler" not in requirements


@pytest.mark.unit
@pytest.mark.parametrize(
    "forbidden", ["l9-semantic-compiler-engine", "SemanticSpec", "TargetIR", "CompilationReceipt"]
)
def test_inserted_compiler_prerequisite_is_a_policy_failure(forbidden):
    with pytest.raises(AssertionError, match=forbidden):
        _assert_native_dependencies(["EIE native validation", forbidden, "CEG native validation"])
