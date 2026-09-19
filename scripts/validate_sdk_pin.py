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
"""

from __future__ import annotations

import re
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


def main() -> int:
    errors = check_tree(ROOT)
    if errors:
        print("FAIL")
        print("\n".join(errors))
        return 1
    print(f"PASS EIE pin {CANONICAL_REPO}@{MAJOR_TAG} -> {resolved_commit(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
