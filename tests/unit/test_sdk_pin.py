"""The Gate_SDK release-identity validator must discriminate, not just pass.

EIE ran three different Gate_SDK identities at once — 69c6c67 in
``pyproject.toml`` and the CI requirements file, ead0f48 in the packet/envelope
gate step of ``pr-pipeline.yml`` (EIE-001). Now there is one declaration
(pyproject ``[tool.uv.sources]`` on tag ``v1``), one lock (``uv.lock``, which
records the resolved commit) and no direct install anywhere else. These tests
drive the failure side: sha, branch, fork, a URL declaration, a lock off the
tag, a direct install in a workflow or Dockerfile, and a lock left behind by a
moving channel.

Nothing here touches the network. ``resolve_remote_tag`` is the only networked
function and is deliberately isolated; the agreement it feeds is exercised as
pure logic.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = REPO_ROOT / "scripts" / "validate_sdk_pin.py"

_SPEC = importlib.util.spec_from_file_location("validate_sdk_pin", VALIDATOR)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"{VALIDATOR} did not load")
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

check_manifest = _MOD.check_manifest
check_lock = _MOD.check_lock
check_install_surface = _MOD.check_install_surface
check_tree = _MOD.check_tree
lock_resolution = _MOD.lock_resolution
compare_lock_to_tag = _MOD.compare_lock_to_tag
safe_remote = _MOD.safe_remote

CHANNEL_OBJECT = "e9f829f982110be13752da8f18c7a9692e8ed908"
STALE_OBJECT = "69c6c67060b08440734a61473c03663423709964"
WORKFLOW_OBJECT = "ead0f48166f510683e9dec6ff7383258cc4307f2"

SOURCE_V1 = '{ git = "https://github.com/Quantum-L9/Gate_SDK.git", tag = "v1" }'
MANIFEST_V1 = (
    '[project]\nname = "x"\ndependencies = ["fastapi>=0.115.0", "constellation-node-sdk"]\n\n'
    f"[tool.uv.sources]\nconstellation-node-sdk = {SOURCE_V1}\n"
)
LOCK_V1 = (
    "[[package]]\n"
    'name = "constellation-node-sdk"\n'
    'version = "1.3.2"\n'
    'source = { git = "https://github.com/Quantum-L9/Gate_SDK.git'
    f'?tag=v1#{CHANNEL_OBJECT}" }}\n'
)
WORKFLOW_LOCKED = "      - run: bash tools/deps/install_locked.sh full\n"
WORKFLOW_DIRECT = (
    "      - run: |\n"
    '          python -m pip install "constellation-node-sdk @ '
    'git+https://github.com/Quantum-L9/Gate_SDK.git@v1"\n'
)


# ─────────────────────────────────────────────── the one declaration


def test_the_moving_major_channel_passes() -> None:
    assert check_manifest("pyproject.toml", MANIFEST_V1) == []


@pytest.mark.parametrize("sha", [STALE_OBJECT, WORKFLOW_OBJECT])
def test_a_commit_sha_declaration_fails(sha: str) -> None:
    """Both shas EIE was actually running must now be rejected."""
    errors = check_manifest("pyproject.toml", MANIFEST_V1.replace('tag = "v1"', f'rev = "{sha}"'))
    assert any("commit sha" in item for item in errors), errors


def test_the_exact_release_tag_is_not_the_consumer_contract() -> None:
    errors = check_manifest("pyproject.toml", MANIFEST_V1.replace('tag = "v1"', 'tag = "v1.1.0"'))
    assert any("not 'v1'" in item for item in errors), errors


@pytest.mark.parametrize("branch", ["main", "master"])
def test_a_floating_branch_fails(branch: str) -> None:
    errors = check_manifest(
        "pyproject.toml", MANIFEST_V1.replace('tag = "v1"', f'branch = "{branch}"')
    )
    assert any("floats on branch" in item for item in errors), errors


def test_the_forbidden_fork_fails() -> None:
    forked = MANIFEST_V1.replace("Quantum-L9/Gate_SDK", "cryptoxdog/Gate_SDK")
    errors = check_manifest("pyproject.toml", forked)
    assert any("forbidden fork" in item for item in errors), errors


def test_a_direct_url_declaration_fails() -> None:
    """The source lives in [tool.uv.sources]; a PEP 508 URL is a second identity."""
    direct = MANIFEST_V1.replace(
        '"constellation-node-sdk"]',
        '"constellation-node-sdk @ git+https://github.com/Quantum-L9/Gate_SDK.git@v1"]',
    )
    errors = check_manifest("pyproject.toml", direct)
    assert any("name it bare" in item for item in errors), errors


def test_a_manifest_with_no_sdk_dependency_fails() -> None:
    errors = check_manifest("pyproject.toml", '[project]\ndependencies = ["fastapi>=0.115.0"]\n')
    assert any("is not a [project] dependency" in item for item in errors), errors
    assert any("no [tool.uv.sources] entry" in item for item in errors), errors


# ─────────────────────────────────────────────────────────── lock


def test_the_lock_must_record_the_tag_and_a_concrete_commit() -> None:
    assert check_lock("uv.lock", LOCK_V1) == []
    assert lock_resolution(LOCK_V1) == CHANNEL_OBJECT


def test_a_lock_without_a_resolved_commit_fails() -> None:
    unresolved = LOCK_V1.replace(f"#{CHANNEL_OBJECT}", "")
    errors = check_lock("uv.lock", unresolved)
    assert any("concrete 40-character commit" in item for item in errors), errors
    assert lock_resolution(unresolved) is None


def test_a_lock_off_the_major_tag_fails() -> None:
    errors = check_lock("uv.lock", LOCK_V1.replace("?tag=v1#", "?branch=main#"))
    assert any("want tag='v1'" in item for item in errors), errors


# ──────────────────────────────────────────────────── install sites


def test_an_install_from_the_lock_passes() -> None:
    assert check_install_surface(".github/workflows/pr-pipeline.yml", WORKFLOW_LOCKED) == []


@pytest.mark.parametrize("ref", ["v1", WORKFLOW_OBJECT])
def test_a_direct_sdk_install_in_a_workflow_fails(ref: str) -> None:
    """The third identity lived here precisely because nothing read it."""
    errors = check_install_surface(
        ".github/workflows/pr-pipeline.yml", WORKFLOW_DIRECT.replace("@v1", f"@{ref}")
    )
    assert any("installs Gate_SDK directly" in item for item in errors), errors


def test_a_hashed_archive_install_in_a_dockerfile_fails() -> None:
    line = (
        f"RUN pip install https://github.com/Quantum-L9/Gate_SDK/archive/{CHANNEL_OBJECT}.tar.gz\n"
    )
    errors = check_install_surface("Dockerfile.prod", line)
    assert any("installs Gate_SDK directly" in item for item in errors), errors


# ────────────────────────────────────────────── surface enumeration


def _seed(root: Path) -> None:
    (root / "pyproject.toml").write_text(MANIFEST_V1)
    (root / "uv.lock").write_text(LOCK_V1)
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "pr-pipeline.yml").write_text(WORKFLOW_LOCKED)


def test_a_complete_tree_passes(tmp_path: Path) -> None:
    _seed(tmp_path)
    assert check_tree(tmp_path) == []


def test_every_workflow_and_dockerfile_is_an_install_surface(tmp_path: Path) -> None:
    """An un-enumerated install site is how a third Gate_SDK sha ran unnoticed."""
    _seed(tmp_path)
    (tmp_path / ".github" / "workflows" / "new-gate.yml").write_text(WORKFLOW_DIRECT)
    (tmp_path / "Dockerfile.dev").write_text(WORKFLOW_DIRECT)
    errors = check_tree(tmp_path)
    assert any(e.startswith(".github/workflows/new-gate.yml:") for e in errors), errors
    assert any(e.startswith("Dockerfile.dev:") for e in errors), errors


@pytest.mark.parametrize("missing", ["pyproject.toml", "uv.lock"])
def test_a_missing_manifest_or_lock_fails(tmp_path: Path, missing: str) -> None:
    _seed(tmp_path)
    (tmp_path / missing).unlink()
    errors = check_tree(tmp_path)
    assert f"{missing}: missing" in errors, errors


def test_the_real_tree_declares_the_channel() -> None:
    assert check_tree(REPO_ROOT) == []


# ──────────────────────────────── stale lock (--verify-tag logic)


def test_a_current_lock_agrees_with_the_channel() -> None:
    assert compare_lock_to_tag(CHANNEL_OBJECT, CHANNEL_OBJECT) == []


def test_a_stale_lock_fails() -> None:
    errors = compare_lock_to_tag(STALE_OBJECT, CHANNEL_OBJECT)
    assert any("stale" in item for item in errors), errors


def test_an_unresolvable_channel_fails_closed() -> None:
    """Required networked mode: no resolution is a failure, never a pass."""
    errors = compare_lock_to_tag(CHANNEL_OBJECT, None)
    assert any("could not resolve" in item for item in errors), errors


def test_a_lock_with_no_resolution_fails_closed() -> None:
    errors = compare_lock_to_tag(None, CHANNEL_OBJECT)
    assert any("no concrete resolved commit" in item for item in errors), errors


# ── remote validation (SonarCloud pythonsecurity:S8705) ──────────────────────
#
# argv is a list and no shell is involved, which stops command injection but
# not argument injection: `git ls-remote --upload-pack=<cmd> <repo>` executes
# <cmd>, so a --remote beginning with `-` is an execution vector by itself.


def test_a_canonical_remote_is_accepted(tmp_path: Path) -> None:
    url = "https://github.com/Quantum-L9/Gate_SDK.git"
    assert safe_remote(url) == url
    # An existing directory is accepted so tests can use a fixture repo.
    assert safe_remote(str(tmp_path)) == str(tmp_path.resolve())


@pytest.mark.parametrize("hostile", ["--upload-pack=touch /tmp/pwned", "-u", "--exec=sh", ""])
def test_a_remote_git_would_read_as_an_option_is_refused(hostile: str) -> None:
    with pytest.raises(ValueError, match="must not begin with"):
        safe_remote(hostile)


def test_a_non_canonical_remote_that_is_not_a_repository_is_refused() -> None:
    with pytest.raises(ValueError, match="not canonical and not a local repository"):
        safe_remote("https://example.invalid/evil/Gate_SDK.git")
