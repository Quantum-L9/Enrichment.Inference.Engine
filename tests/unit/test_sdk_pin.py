"""The Gate_SDK release-identity validator must discriminate, not just pass.

EIE ran three different Gate_SDK identities at once — 69c6c67 in
``pyproject.toml`` and ``requirements-ci.txt``, ead0f48 in the packet/envelope
gate step of ``pr-pipeline.yml`` — and the old validator could not see the
third, because it only read manifests. These tests drive the failure side of
the replacement: sha, branch, fork, an un-enumerated surface, an unhashed lock,
and a lock left behind by a moving channel.

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
check_tree = _MOD.check_tree
lock_resolution = _MOD.lock_resolution
compare_lock_to_tag = _MOD.compare_lock_to_tag
safe_remote = _MOD.safe_remote
MANIFESTS = _MOD.MANIFESTS

CHANNEL_OBJECT = "e9f829f982110be13752da8f18c7a9692e8ed908"
STALE_OBJECT = "69c6c67060b08440734a61473c03663423709964"
WORKFLOW_OBJECT = "ead0f48166f510683e9dec6ff7383258cc4307f2"
ARCHIVE_DIGEST = "e" * 64

MANIFEST_V1 = "constellation-node-sdk @ git+https://github.com/Quantum-L9/Gate_SDK.git@v1\n"
WORKFLOW_V1 = (
    "          python -m pip install \\\n"
    '            "constellation-node-sdk @ '
    'git+https://github.com/Quantum-L9/Gate_SDK.git@v1"  # NOSONAR\n'
)
LOCK_LINE = (
    f"constellation-node-sdk @ https://github.com/Quantum-L9/Gate_SDK/archive/"
    f"{CHANNEL_OBJECT}.tar.gz --hash=sha256:{ARCHIVE_DIGEST}\n"
)


# ─────────────────────────────────────────────── active declarations


def test_the_moving_major_channel_passes() -> None:
    assert check_manifest("pyproject.toml", MANIFEST_V1) == []
    assert check_manifest("requirements-ci.txt", MANIFEST_V1) == []
    assert check_manifest(".github/workflows/pr-pipeline.yml", WORKFLOW_V1) == []


@pytest.mark.parametrize("sha", [STALE_OBJECT, WORKFLOW_OBJECT])
def test_a_commit_sha_declaration_fails(sha: str) -> None:
    """Both shas EIE was actually running must now be rejected."""
    errors = check_manifest("pyproject.toml", MANIFEST_V1.replace("@v1", f"@{sha}"))
    assert any("commit sha" in item for item in errors), errors


def test_the_workflow_install_is_checked_like_any_other_surface() -> None:
    """The third identity lived here precisely because nothing read it."""
    assert ".github/workflows/pr-pipeline.yml" in MANIFESTS
    drifted = WORKFLOW_V1.replace("@v1", f"@{WORKFLOW_OBJECT}")
    errors = check_manifest(".github/workflows/pr-pipeline.yml", drifted)
    assert any("commit sha" in item for item in errors), errors


def test_the_exact_release_tag_is_not_the_consumer_contract() -> None:
    errors = check_manifest("pyproject.toml", MANIFEST_V1.replace("@v1", "@v1.1.0"))
    assert any("not 'v1'" in item for item in errors), errors


@pytest.mark.parametrize("branch", ["main", "master"])
def test_a_floating_branch_fails(branch: str) -> None:
    errors = check_manifest("pyproject.toml", MANIFEST_V1.replace("@v1", f"@{branch}"))
    assert any("floats on branch" in item for item in errors), errors


def test_the_forbidden_fork_fails() -> None:
    forked = MANIFEST_V1.replace("Quantum-L9/Gate_SDK", "cryptoxdog/Gate_SDK")
    errors = check_manifest("pyproject.toml", forked)
    assert any("forbidden fork" in item for item in errors), errors


def test_a_surface_with_no_sdk_declaration_fails() -> None:
    errors = check_manifest("requirements-ci.txt", "fastapi>=0.115.0\n")
    assert any("no Quantum-L9/Gate_SDK declaration" in item for item in errors), errors


# ─────────────────────────────────────────────────────────── lock


def test_the_lock_must_carry_a_concrete_hash_verified_archive() -> None:
    assert check_lock("requirements.lock", LOCK_LINE) == []
    assert lock_resolution(LOCK_LINE) == CHANNEL_OBJECT


def test_an_unhashed_git_lock_entry_fails() -> None:
    errors = check_lock("requirements.lock", MANIFEST_V1)
    assert any("hash-verified" in item for item in errors), errors
    assert lock_resolution(MANIFEST_V1) is None


# ────────────────────────────────────────────── surface enumeration


def _seed(root: Path) -> None:
    (root / "pyproject.toml").write_text(MANIFEST_V1)
    (root / "requirements-ci.txt").write_text(MANIFEST_V1)
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "pr-pipeline.yml").write_text(WORKFLOW_V1)
    (root / "requirements.lock").write_text(LOCK_LINE)


def test_a_complete_tree_passes(tmp_path: Path) -> None:
    _seed(tmp_path)
    assert check_tree(tmp_path) == []


def test_a_missing_surface_fails_rather_than_being_skipped(tmp_path: Path) -> None:
    """Skipping an absent surface is how the workflow sha stayed invisible."""
    _seed(tmp_path)
    (tmp_path / ".github" / "workflows" / "pr-pipeline.yml").unlink()
    errors = check_tree(tmp_path)
    assert any("pr-pipeline.yml: active surface is missing" in e for e in errors), errors


def test_a_missing_lock_fails(tmp_path: Path) -> None:
    _seed(tmp_path)
    (tmp_path / "requirements.lock").unlink()
    errors = check_tree(tmp_path)
    assert any("requirements.lock: generated lock is missing" in e for e in errors), errors


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
    assert any("no concrete resolved object" in item for item in errors), errors


# ── remote validation (SonarCloud pythonsecurity:S8705) ──────────────────────
#
# argv is a list and no shell is involved, which stops command injection but
# not argument injection: `git ls-remote --upload-pack=<cmd> <repo>` executes
# <cmd>, so a --remote beginning with `-` is an execution vector by itself.


def test_a_canonical_remote_is_accepted() -> None:
    url = "https://github.com/Quantum-L9/Gate_SDK.git"
    assert safe_remote(url) == url
    # Local paths are accepted so tests can resolve against a fixture repo.
    assert safe_remote("/tmp/fixture-origin") == "/tmp/fixture-origin"


@pytest.mark.parametrize("hostile", ["--upload-pack=touch /tmp/pwned", "-u", "--exec=sh", ""])
def test_a_remote_git_would_read_as_an_option_is_refused(hostile: str) -> None:
    with pytest.raises(ValueError, match="must not begin with"):
        safe_remote(hostile)
