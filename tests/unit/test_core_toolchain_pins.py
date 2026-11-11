"""Core-owned tools sit on one version across every EIE file.

Quantum-L9/l9-ci-core owns the versions of the shared tools
(.github/actions/install-consumer-ci/toolchain-lock.json); version drift from
Core is prohibited org-wide. The required ``Analyze (central Core)`` check
compares EIE's declarations with that lock, and only warns. These tests make
the rule fail from EIE's side without copying the Core lock: every declaration
of a Core-owned tool (pyproject.toml [project] and [dependency-groups],
uv.lock, the pre-commit rev comments) must name the same version, so Core's
comparison of any one of them covers all of them.

They also keep the Dependabot rules that stop drift recurring: the file is the
org render, Core-owned tools are ignored, semver-major bumps are never
proposed, minor/patch bumps arrive grouped, and every github/codeql-action
step runs one version.
"""

from __future__ import annotations

import re
import tomllib
from collections import defaultdict
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit

# Core-owned tools EIE declares through Python packaging (biome, python and pip
# are pinned elsewhere and have no EIE package declaration to compare; semgrep
# is installed ad hoc by the Semgrep jobs and has no declaration either).
CORE_TOOLS = ("pytest", "ruff", "mypy", "pre-commit", "semgrep")

_PRECOMMIT_REPOS = {
    "https://github.com/astral-sh/ruff-pre-commit": "ruff",
    "https://github.com/pre-commit/mirrors-mypy": "mypy",
}


def _declarations() -> dict[str, set[tuple[str, str]]]:
    """Map each Core-owned tool to the (version, source) pairs EIE declares."""
    found: dict[str, set[tuple[str, str]]] = defaultdict(set)

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sections = {"project.dependencies": pyproject.get("project", {}).get("dependencies", [])}
    sections.update(
        {
            f"dependency-groups.{name}": specs
            for name, specs in pyproject.get("dependency-groups", {}).items()
        }
    )
    for section, specs in sections.items():
        for spec in specs:
            req = Requirement(spec)
            name = canonicalize_name(req.name)
            if name not in CORE_TOOLS:
                continue
            exact = [s.version for s in req.specifier if s.operator in ("==", "===")]
            version = (
                exact[0]
                if len(exact) == 1 and len(req.specifier) == 1
                else f"non-exact:{req.specifier}"
            )
            found[name].add((version, f"pyproject.toml [{section}]"))

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    for package in lock.get("package", []):
        if canonicalize_name(package["name"]) in CORE_TOOLS:
            found[canonicalize_name(package["name"])].add((package["version"], "uv.lock"))

    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    raw_lines = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8").splitlines()
    for repo in config.get("repos", []):
        tool = _PRECOMMIT_REPOS.get(repo.get("repo", ""))
        if tool is None:
            continue
        rev_line = next(line for line in raw_lines if repo["rev"] in line)
        match = re.search(r"#\s*v?(\d+(?:\.\d+)*)", rev_line)
        version = match.group(1) if match else f"unlabelled-rev:{repo['rev']}"
        found[tool].add((version, ".pre-commit-config.yaml"))

    return found


def test_every_core_owned_tool_has_a_single_version() -> None:
    drift = {
        tool: sorted(pairs)
        for tool, pairs in _declarations().items()
        if len({version for version, _ in pairs}) > 1
    }
    assert not drift, (
        "Core-owned tool declared at more than one version; align every file to "
        f"Quantum-L9/l9-ci-core toolchain-lock.json: {drift}"
    )


def test_core_owned_tools_are_declared_somewhere() -> None:
    # Guards the scan itself: if it stopped finding the tools EIE pins, the
    # single-version test above would pass vacuously.
    found = _declarations()
    assert {"pytest", "ruff", "mypy", "pre-commit"} <= set(found), sorted(found)


def test_dependabot_ignores_core_owned_tools() -> None:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    # uv when uv.lock exists (the org render's rule), which is the case here.
    python = next(u for u in config["updates"] if u["package-ecosystem"] == "uv")
    ignored = {
        entry["dependency-name"].lower()
        for entry in python.get("ignore", [])
        if "versions" not in entry
    }
    assert set(CORE_TOOLS) <= ignored, (
        f"Dependabot must not propose Core-owned tools: missing {set(CORE_TOOLS) - ignored}"
    )


def test_dependabot_is_the_org_render() -> None:
    # Quantum-L9/.github renders and restores this file (ADR-0019); a hand edit
    # here is replaced on the next seed, so a policy change belongs there.
    text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert text.startswith("# Rendered by Quantum-L9/.github ops/render-dependabot.js"), text[:120]


def test_dependabot_opens_only_safe_prs() -> None:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    for update in config["updates"]:
        ecosystem = update["package-ecosystem"]
        majors = [
            entry
            for entry in update.get("ignore", [])
            if entry.get("dependency-name") == "*"
            and entry.get("update-types") == ["version-update:semver-major"]
        ]
        assert majors, f"{ecosystem}: semver-major bumps must be ignored (safe-only)"
        groups = update.get("groups", {}).values()
        assert any(
            "*" in g.get("patterns", []) and g.get("update-types") == ["minor", "patch"]
            for g in groups
        ), f"{ecosystem}: minor/patch bumps must land as one grouped PR"


def test_dependabot_bumps_every_codeql_action_step_together() -> None:
    # A split bump fails CodeQL ("Loaded a configuration file for version X, but
    # running version Y"). The github-actions minor/patch group matches "*", so
    # init, analyze and upload-sarif always move in one PR; majors are ignored.
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    actions = next(u for u in config["updates"] if u["package-ecosystem"] == "github-actions")
    patterns = [
        p for group in actions.get("groups", {}).values() for p in group.get("patterns", [])
    ]
    assert "*" in patterns or "github/codeql-action*" in patterns, patterns


def test_every_codeql_action_step_runs_one_version() -> None:
    refs = set()
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        refs.update(
            re.findall(
                r"uses:\s*github/codeql-action/[\w-]+@(\S+)", workflow.read_text(encoding="utf-8")
            )
        )
    assert refs, "no github/codeql-action step found; the scan above is broken"
    assert len(refs) == 1, f"github/codeql-action steps must share one ref, found {sorted(refs)}"
