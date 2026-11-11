"""Every package a workflow installs is hash-verified (Scorecard Pinned-Dependencies, #215).

Workflows install Python packages through tools/deps/install_locked.sh, which
installs from uv.lock with every distribution checked against its locked
sha256, or through pip with --require-hashes. A bare `pip install <name>` is
an unpinned pipCommand: the version and the bytes are whatever the index
serves that day.

Single-purpose tools (release: build) are dependency groups in
pyproject.toml, so they are locked and hashed too, and install_locked.sh keeps
them out of the development (`full`) profile.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
INSTALLER = ROOT / "tools" / "deps" / "install_locked.sh"

# `pip install semgrep` (ci.yml, pr-pipeline.yml) is replaced by Core's
# installer in PR #312. Remove that entry when it lands.
# `cyclonedx-bom` (supply-chain.yml) is not locked: it would add chardet
# (LGPL-2.1-or-later) and rfc3987-syntax to uv.lock, which the permissive-only
# Dependency Review policy (2026-11-11) rejects. PR #318 replaces it with Syft.
# Remove that entry when it lands.
PENDING_ELSEWHERE = {"pip install semgrep", "pip install 'cyclonedx-bom==7.3.0'"}

PIP_INSTALL = re.compile(r"(?:python3?\s+-m\s+)?pip\s+install\b[^\n]*")


def _run_lines() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job in (workflow.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                for line in str(step.get("run", "")).splitlines():
                    if not line.strip().startswith("#"):
                        found.append((path.name, line.strip()))
    return found


#: pip options that consume the next argument, so it is not an install target.
_OPTIONS_WITH_VALUE = {"-r", "--requirement", "-c", "--constraint", "--python", "-t", "--target"}


def _installs_only_local_paths(command: str) -> bool:
    """True for ``pip install --no-deps <path>...``: nothing is fetched from an index.

    A local checkout (for example a peer repository verified against its commit
    SHA) installed without dependency resolution downloads nothing, so there is
    nothing for a hash to pin. Every target must be a path, and ``--no-deps``
    must be present, or pip would resolve dependencies from the index unhashed.
    """
    tokens = shlex.split(command)
    targets: list[str] = []
    skip = False
    for token in tokens[tokens.index("install") + 1 :]:
        if skip:
            skip = False
            continue
        if token in _OPTIONS_WITH_VALUE:
            skip = True
        elif not token.startswith("-"):
            targets.append(token)
    return (
        "--no-deps" in tokens
        and bool(targets)
        and all(target.startswith((".", "/", "$")) for target in targets)
    )


def test_no_workflow_runs_an_unhashed_pip_install() -> None:
    unhashed = [
        (name, match.group(0))
        for name, line in _run_lines()
        for match in [PIP_INSTALL.search(line)]
        if match
        and "--require-hashes" not in line
        and match.group(0).strip() not in PENDING_ELSEWHERE
        and not _installs_only_local_paths(match.group(0))
    ]
    assert not unhashed, (
        f"install through tools/deps/install_locked.sh or --require-hashes: {unhashed}"
    )


def _single_purpose_groups() -> list[str]:
    text = INSTALLER.read_text(encoding="utf-8")
    match = re.search(r"^SINGLE_PURPOSE_GROUPS=\(([^)]*)\)", text, re.MULTILINE)
    assert match, "install_locked.sh must declare SINGLE_PURPOSE_GROUPS"
    return match.group(1).split()


def test_single_purpose_groups_are_declared_dependency_groups() -> None:
    groups = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "dependency-groups"
    ]
    declared = _single_purpose_groups()
    assert declared == ["release"]
    assert set(declared) <= set(groups)


def test_full_profile_excludes_the_single_purpose_groups() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    full = re.search(r"^\s*full\)\n(.*?);;", text, re.MULTILINE | re.DOTALL)
    assert full, "install_locked.sh must define the full profile"
    assert "--all-groups" in full.group(1)
    assert '--no-group "$group"' in full.group(1)
    assert "SINGLE_PURPOSE_GROUPS" in full.group(1)


@pytest.mark.parametrize(
    ("command", "local"),
    [
        ('pip install --disable-pip-version-check --no-deps -e "$gate/constellation-gate"', True),
        ("pip install --no-deps .", True),
        ('pip install -e "$gate/constellation-gate"', False),  # resolves deps from the index
        ("pip install --no-deps pyyaml", False),  # a registry name, unhashed
        ("pip install --no-deps -r requirements.txt", False),  # no path target
    ],
)
def test_local_path_rule(command: str, local: bool) -> None:
    assert _installs_only_local_paths(command) is local
