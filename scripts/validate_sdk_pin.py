#!/usr/bin/env python3
"""Fail closed unless the Gate_SDK pin is consistent across manifests and the lock.

Release-set policy (matches Cognitive.Engine.Graphs/scripts/validate_sdk_pin.py):

* **Manifests** (``pyproject.toml``, ``requirements-ci.txt``) name the moving
  major tag ``Quantum-L9/Gate_SDK@v1``. They never carry a 40-character commit.
* **The lock** (``requirements.lock``) names the concrete commit that tag
  resolved to, as GitHub's source archive URL with its sha256 — pip refuses a
  ``git+https`` requirement outright in ``--require-hashes`` mode, so the
  archive form is the only hash-verifiable one. ``tools/lock_requirements.sh``
  writes it.

The earlier version of this script checked the manifests only. That is how
EIE-001 happened: ``pyproject.toml`` said 69c6c67 while ``requirements.lock``
said 2b2f53a2, two commits that differ by the ``GateClientConfig`` fix loading
``L9_VERIFYING_KEYS_JSON``, so the production image ran SDK code CI never
installed and the validator reported PASS throughout. Checking both files is
the point of this script, not a detail of it.

Two modes, because they have different costs:

* **Default** — files only, offline, safe for pre-commit and every CI job. It
  proves the manifests name ``v1`` and the lock carries a hash-verifiable
  commit.
* **``--verify-tag``** — additionally resolves ``v1`` against the canonical
  remote and fails if it no longer points at the commit the lock pins. This is
  the check the moving-tag policy actually needs: the two files can each be
  internally valid and still describe different SDKs the moment the tag
  advances. It needs network, so it belongs in a scheduled or release job
  rather than in the pre-commit path.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAJOR_TAG = "v1"
CANONICAL_REPO = "Quantum-L9/Gate_SDK"
FORBIDDEN_FORK = "cryptoxdog/Gate_SDK"

MANIFESTS = ("pyproject.toml", "requirements.txt", "requirements-ci.txt")
LOCK = "requirements.lock"

MANIFEST_PIN_RE = re.compile(
    r"constellation-node-sdk @ git\+https://github\.com/Quantum-L9/Gate_SDK\.git@(?P<ref>[^\s\"',]+)"
)
LOCK_PIN_RE = re.compile(
    r"constellation-node-sdk @ https://github\.com/Quantum-L9/Gate_SDK/archive/"
    r"(?P<sha>[0-9a-f]{40})\.tar\.gz\s+--hash=sha256:(?P<digest>[0-9a-f]{64})"
)


def check_manifest(rel: str, text: str) -> list[str]:
    errors: list[str] = []
    if FORBIDDEN_FORK in text:
        errors.append(f"{rel}: {FORBIDDEN_FORK} remains")
    match = MANIFEST_PIN_RE.search(text)
    if match is None:
        errors.append(f"{rel}: no constellation-node-sdk pin on {CANONICAL_REPO}")
        return errors
    ref = match.group("ref")
    if ref != MAJOR_TAG:
        errors.append(f"{rel}: pinned to {ref!r} (want the major tag {MAJOR_TAG!r})")
    return errors


def check_lock(rel: str, text: str) -> list[str]:
    errors: list[str] = []
    if FORBIDDEN_FORK in text:
        errors.append(f"{rel}: {FORBIDDEN_FORK} remains")
    if MANIFEST_PIN_RE.search(text):
        # pip rejects a VCS requirement in hash-checking mode, so a git+ line
        # here means the lock cannot install at all.
        errors.append(f"{rel}: git+https requirement is not hash-verifiable")
    if LOCK_PIN_RE.search(text) is None:
        errors.append(
            f"{rel}: no hashed {CANONICAL_REPO} source archive "
            "(regenerate with `bash tools/lock_requirements.sh`)"
        )
    return errors


def check_tree(root: Path) -> list[str]:
    errors: list[str] = []
    seen_manifest = False
    for rel in MANIFESTS:
        path = root / rel
        if not path.exists():
            continue
        seen_manifest = True
        errors.extend(check_manifest(rel, path.read_text(encoding="utf-8")))
    if not seen_manifest:
        errors.append("no manifest declaring constellation-node-sdk found")

    lock_path = root / LOCK
    if not lock_path.exists():
        errors.append(f"{LOCK}: missing — the production image installs from it")
    else:
        errors.extend(check_lock(LOCK, lock_path.read_text(encoding="utf-8")))
    return errors


def resolved_commit(root: Path) -> str | None:
    lock_path = root / LOCK
    if not lock_path.exists():
        return None
    match = LOCK_PIN_RE.search(lock_path.read_text(encoding="utf-8"))
    return None if match is None else match.group("sha")


def tag_commit(timeout: float = 30.0) -> str:
    """The commit ``MAJOR_TAG`` points at right now, per the canonical remote.

    Raises ``RuntimeError`` rather than returning a sentinel: a resolution this
    script could not perform must never read as agreement.
    """
    url = f"https://github.com/{CANONICAL_REPO}.git"
    # S603/S607: fixed argv, no shell, and the URL is built from module
    # constants — nothing here is caller-supplied. Rationale kept off the
    # directive lines: prose after the codes is not valid `noqa` syntax.
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "ls-remote", url, MAJOR_TAG, f"refs/tags/{MAJOR_TAG}^{{}}"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        msg = f"could not resolve {CANONICAL_REPO}@{MAJOR_TAG}: {exc}"
        raise RuntimeError(msg) from exc

    # An annotated tag lists both the tag object and, as `^{}`, the commit it
    # dereferences to. The commit is what an install actually checks out, so it
    # wins when both are present.
    shas = {ref: sha for sha, _, ref in (line.partition("\t") for line in out.splitlines()) if sha}
    sha = shas.get(f"refs/tags/{MAJOR_TAG}^{{}}") or shas.get(f"refs/tags/{MAJOR_TAG}")
    if not sha:
        msg = f"{CANONICAL_REPO} has no tag {MAJOR_TAG!r}"
        raise RuntimeError(msg)
    return sha


def check_tag_agreement(root: Path) -> list[str]:
    """Does the tag the manifests name still point at the commit the lock pins?

    The gap this closes (PR #212 review): ``check_tree`` proves the manifests
    say ``v1`` and that the lock carries *a* hashed commit — never that the two
    describe the same SDK. Under a moving tag they diverge the moment ``v1``
    advances: CI and dev installs resolve the new commit from the manifests
    while the production image keeps installing the old one from the lock.
    That is EIE-001 again, reached from the other direction, and the file-only
    checks cannot see it.

    Network-dependent, so it is opt-in (``--verify-tag``) rather than folded
    into ``check_tree``, which must stay runnable offline and in pre-commit.
    Refresh a stale lock with ``bash tools/lock_requirements.sh``.
    """
    pinned = resolved_commit(root)
    if pinned is None:
        return [f"{LOCK}: no resolved commit to compare against {MAJOR_TAG!r}"]
    try:
        current = tag_commit()
    except RuntimeError as exc:
        return [str(exc)]
    if current != pinned:
        return [
            f"{MAJOR_TAG!r} now resolves to {current} but {LOCK} pins {pinned} — "
            "manifests and the production image would install different SDKs "
            "(regenerate with `bash tools/lock_requirements.sh`)"
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the Gate_SDK release-set pin.")
    parser.add_argument(
        "--verify-tag",
        action="store_true",
        help=(
            f"also resolve {CANONICAL_REPO}@{MAJOR_TAG} against the remote and fail if it no "
            f"longer points at the commit {LOCK} pins. Requires network."
        ),
    )
    args = parser.parse_args(argv)

    errors = check_tree(ROOT)
    if args.verify_tag:
        errors.extend(check_tag_agreement(ROOT))
    if errors:
        print("FAIL")
        print("\n".join(errors))
        return 1

    resolved = resolved_commit(ROOT)
    suffix = " (tag agreement verified)" if args.verify_tag else ""
    print(f"PASS EIE pin {CANONICAL_REPO}@{MAJOR_TAG} -> {resolved}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
