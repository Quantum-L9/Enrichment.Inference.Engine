"""EIE-001 — the Gate_SDK pin must agree between the manifests and the lock.

The defect this locks out: ``pyproject.toml`` named commit 69c6c67 while
``requirements.lock`` named 2b2f53a2. Those two commits differ by the
``GateClientConfig`` fix that loads ``L9_VERIFYING_KEYS_JSON`` from the
environment, so ``Dockerfile.prod`` installed SDK code CI never exercised — and
the validator of the day reported PASS, because it only read the manifests.

Release-set policy: manifests name the moving major tag ``@v1``; the lock names
the concrete commit that tag resolved to, as a hash-verified source archive
(pip refuses a ``git+https`` requirement in ``--require-hashes`` mode).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "validate_sdk_pin", ROOT / "scripts" / "validate_sdk_pin.py"
    )
    if spec is None or spec.loader is None:
        msg = "scripts/validate_sdk_pin.py could not be loaded"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


def test_repository_tree_passes_the_validator() -> None:
    assert validator.check_tree(ROOT) == []


def test_manifests_name_the_major_tag_not_a_commit() -> None:
    for rel in ("pyproject.toml", "requirements-ci.txt"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        match = validator.MANIFEST_PIN_RE.search(text)
        assert match is not None, f"{rel} declares no Gate_SDK pin"
        assert match.group("ref") == validator.MAJOR_TAG


def test_lock_names_a_hash_verified_commit() -> None:
    text = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    match = validator.LOCK_PIN_RE.search(text)
    assert match is not None, "requirements.lock carries no hashed Gate_SDK archive"
    assert re.fullmatch(r"[0-9a-f]{40}", match.group("sha"))


def _tree(tmp_path: Path, *, manifest: str, lock: str | None) -> Path:
    (tmp_path / "pyproject.toml").write_text(manifest, encoding="utf-8")
    if lock is not None:
        (tmp_path / "requirements.lock").write_text(lock, encoding="utf-8")
    return tmp_path


_SDK = "constellation-node-sdk @ git+https://github.com/Quantum-L9/Gate_SDK.git@"
_ARCHIVE = "constellation-node-sdk @ https://github.com/Quantum-L9/Gate_SDK/archive/"
_GOOD_LOCK = (
    f"{_ARCHIVE}e9f829f982110be13752da8f18c7a9692e8ed908.tar.gz "
    "--hash=sha256:eb71f08de7738b48281f430cebca139be2175fd00b056a34ccf4fce423db5f1d\n"
)


def test_rejects_the_original_split(tmp_path: Path) -> None:
    """The exact pre-fix state: a commit in the manifest, another in the lock."""
    tree = _tree(
        tmp_path,
        manifest=f'  "{_SDK}69c6c67060b08440734a61473c03663423709964",\n',
        lock=(
            f"{_ARCHIVE}2b2f53a28a59bbfb2fa45f5eac32b722d802209a.tar.gz "
            "--hash=sha256:bea90bd38c5ecb084daccbdffe92c276ecd5508652fd4dfa659e31d28ff5d387\n"
        ),
    )
    errors = validator.check_tree(tree)
    assert any("major tag" in e for e in errors), errors


def test_accepts_the_release_set_shape(tmp_path: Path) -> None:
    """The positive case — every other test here is a rejection.

    `_GOOD_LOCK` existed but nothing consumed it, so the suite proved the
    validator says no and never proved it says yes. A validator that rejected
    everything would have passed all of them.
    """
    tree = _tree(tmp_path, manifest=f'  "{_SDK}v1",\n', lock=_GOOD_LOCK)
    assert validator.check_tree(tree) == []
    assert validator.resolved_commit(tree) == "e9f829f982110be13752da8f18c7a9692e8ed908"


def test_rejects_a_missing_lock(tmp_path: Path) -> None:
    tree = _tree(tmp_path, manifest=f'  "{_SDK}v1",\n', lock=None)
    assert any("requirements.lock" in e for e in validator.check_tree(tree))


def test_rejects_an_unhashable_git_requirement_in_the_lock(tmp_path: Path) -> None:
    tree = _tree(tmp_path, manifest=f'  "{_SDK}v1",\n', lock=f"{_SDK}v1\n")
    assert any("hash-verifiable" in e for e in validator.check_tree(tree))


@pytest.mark.parametrize("rel", ["pyproject.toml", "requirements-ci.txt", "requirements.lock"])
def test_the_abandoned_fork_is_gone(rel: str) -> None:
    assert validator.FORBIDDEN_FORK not in (ROOT / rel).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# --verify-tag: the moving tag's own failure mode
#
# PR #212 review (Codex P1). check_tree() proves the manifests say `v1` and
# that the lock carries *a* hashed commit — never that the two name the same
# SDK. Under a moving tag they diverge the moment `v1` advances: manifests
# resolve the new commit for CI and dev, the lock keeps installing the old one
# in the production image. That is EIE-001 reached from the other direction,
# and no file-only check can see it. Mocked here; the real resolution is
# network-bound and opt-in.
# --------------------------------------------------------------------------

_PINNED = "e9f829f982110be13752da8f18c7a9692e8ed908"
_ADVANCED = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def _pinned_tree(tmp_path: Path) -> Path:
    return _tree(tmp_path, manifest=f'  "{_SDK}v1",\n', lock=_GOOD_LOCK)


def test_tag_agreement_passes_when_the_tag_still_points_at_the_lock(
    _pinned_tree: Path, monkeypatch
) -> None:
    monkeypatch.setattr(validator, "tag_commit", lambda: _PINNED)
    assert validator.check_tag_agreement(_pinned_tree) == []


def test_tag_agreement_fails_once_the_tag_advances(_pinned_tree: Path, monkeypatch) -> None:
    monkeypatch.setattr(validator, "tag_commit", lambda: _ADVANCED)
    errors = validator.check_tag_agreement(_pinned_tree)
    assert len(errors) == 1
    # Both commits named, so the reader can see which way the drift went.
    assert _ADVANCED in errors[0] and _PINNED in errors[0]
    assert "lock_requirements.sh" in errors[0]


def test_unresolvable_tag_is_an_error_not_a_pass(_pinned_tree: Path, monkeypatch) -> None:
    """No network must never read as agreement — fail closed, not open."""

    def _boom() -> str:
        raise RuntimeError("could not resolve Quantum-L9/Gate_SDK@v1: offline")

    monkeypatch.setattr(validator, "tag_commit", _boom)
    errors = validator.check_tag_agreement(_pinned_tree)
    assert errors and "could not resolve" in errors[0]


def test_tag_agreement_needs_a_lock_to_compare_against(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(validator, "tag_commit", lambda: _PINNED)
    tree = _tree(tmp_path, manifest=f'  "{_SDK}v1",\n', lock=None)
    assert any("no resolved commit" in e for e in validator.check_tag_agreement(tree))


def test_check_tree_stays_offline() -> None:
    """The default path must not reach the network — pre-commit and CI run it.

    Guarded by test rather than convention: folding the remote lookup into
    check_tree() would make every offline run of this validator fail.
    """
    import inspect

    source = inspect.getsource(validator.check_tree)
    assert "tag_commit" not in source
    assert "check_tag_agreement" not in source
