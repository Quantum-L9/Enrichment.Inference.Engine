"""uv.lock is the repository's single dependency lock, and it stays in step with
pyproject.toml.

CI, the devcontainers and both images install only from uv.lock
(tools/deps/install_locked.sh runs `uv sync --locked`), and Dependabot's uv
ecosystem updates pyproject.toml and uv.lock together. These checks are
offline: the networked `uv lock --check` runs in ci.yml (Gate_SDK Channel).
They fail when

* a dependency in pyproject.toml is not what uv.lock was resolved from,
* a registry package in uv.lock has no wheel or no hash (so an install could
  build from source or skip verification),
* a second dependency manifest or lock reappears (requirements*.txt,
  requirements*.lock, poetry.lock, Pipfile*): two places to pin a version is
  drift,
* the pre-commit mypy hook type-checks against package versions the CI
  type-check environment (uv.lock) does not install.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "domain-enrichment-api"
SDK = "constellation-node-sdk"


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _lock() -> dict[str, Any]:
    return tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))


def _root_package() -> dict[str, Any]:
    return next(p for p in _lock()["package"] if p["name"] == PROJECT)


def _from_pyproject(spec: str) -> tuple[str, tuple[str, ...], str]:
    req = Requirement(spec)
    return canonicalize_name(req.name), tuple(sorted(req.extras)), str(req.specifier)


def _from_lock(entry: dict[str, Any]) -> tuple[str, tuple[str, ...], str]:
    return (
        canonicalize_name(entry["name"]),
        tuple(sorted(entry.get("extras", []))),
        entry.get("specifier", ""),
    )


def _normalized(
    items: list[tuple[str, tuple[str, ...], str]],
) -> list[tuple[str, tuple[str, ...], str]]:
    # packaging orders specifier clauses by its own rule; compare clause sets.
    return sorted((n, e, ",".join(sorted(s.split(",")))) for n, e, s in items)


def test_lock_resolves_the_project_dependencies_of_pyproject() -> None:
    declared = _normalized([_from_pyproject(s) for s in _pyproject()["project"]["dependencies"]])
    locked = _normalized([_from_lock(e) for e in _root_package()["metadata"]["requires-dist"]])
    assert declared == locked, (
        "uv.lock is stale: run `uv lock` after editing [project].dependencies"
    )


def test_lock_resolves_every_dependency_group_of_pyproject() -> None:
    groups = _pyproject()["dependency-groups"]
    locked = _root_package()["metadata"]["requires-dev"]
    assert set(groups) == set(locked), (
        "uv.lock is stale: run `uv lock` after adding or removing a group"
    )
    for name, specs in groups.items():
        declared = _normalized([_from_pyproject(s) for s in specs])
        assert declared == _normalized([_from_lock(e) for e in locked[name]]), (
            f"uv.lock is stale for group {name!r}: run `uv lock`"
        )


def test_no_optional_dependency_extras_shadow_the_groups() -> None:
    # `.[dev]` was the old install path; groups replace it, so an extra would
    # be a second, unlocked way to name the same packages.
    assert "optional-dependencies" not in _pyproject()["project"]


def test_lock_matches_the_project_python_range() -> None:
    requires = _pyproject()["project"]["requires-python"]
    assert requires == ">=3.12,<3.13"
    assert _lock()["requires-python"] == "==3.12.*"


def test_gate_sdk_is_a_git_source_on_the_major_tag() -> None:
    source = _pyproject()["tool"]["uv"]["sources"][SDK]
    assert source == {"git": "https://github.com/Quantum-L9/Gate_SDK.git", "tag": "v1"}
    locked = next(p for p in _lock()["package"] if p["name"] == SDK)
    assert re.fullmatch(
        r"https://github\.com/Quantum-L9/Gate_SDK\.git\?tag=v1#[0-9a-f]{40}",
        locked["source"]["git"],
    )


def test_every_registry_package_installs_from_a_hashed_wheel() -> None:
    registry = [p for p in _lock()["package"] if "registry" in p.get("source", {})]
    assert registry, "uv.lock lists no registry packages"
    no_wheel = sorted(p["name"] for p in registry if not p.get("wheels"))
    unhashed = sorted(
        p["name"]
        for p in registry
        if any("hash" not in w for w in [*p.get("wheels", []), p.get("sdist", {})] if w)
    )
    assert not no_wheel, f"would build from source (no wheel locked): {no_wheel}"
    assert not unhashed, f"unhashed distributions in uv.lock: {unhashed}"


def test_no_second_dependency_manifest_or_lock() -> None:
    stray = sorted(
        p.name
        for pattern in ("requirements*.txt", "requirements*.lock", "poetry.lock", "Pipfile*")
        for p in ROOT.glob(pattern)
    )
    assert not stray, (
        f"uv.lock is the only lock and pyproject.toml the only manifest; remove {stray}"
    )


def test_uv_bootstrap_pins_one_hashed_uv() -> None:
    text = (ROOT / "tools" / "deps" / "uv-bootstrap.txt").read_text(encoding="utf-8")
    pins = re.findall(r"^([A-Za-z0-9_.-]+)==(\S+)", text, re.MULTILINE)
    assert [name for name, _ in pins] == ["uv"], pins
    assert len(re.findall(r"--hash=sha256:[0-9a-f]{64}", text)) >= 2


def test_mypy_hook_type_checks_against_the_locked_versions() -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hook = next(
        hook
        for repo in config["repos"]
        if repo.get("repo") == "https://github.com/pre-commit/mirrors-mypy"
        for hook in repo["hooks"]
        if hook["id"] == "mypy"
    )
    locked = {canonicalize_name(p["name"]): p["version"] for p in _lock()["package"]}
    drift = {}
    for spec in hook.get("additional_dependencies", []):
        req = Requirement(spec)
        pinned = [s.version for s in req.specifier if s.operator == "=="]
        name = canonicalize_name(req.name)
        if pinned != [locked.get(name)]:
            drift[name] = (str(req.specifier), locked.get(name))
    assert not drift, f"pre-commit mypy hook pins differ from uv.lock (hook, lock): {drift}"
