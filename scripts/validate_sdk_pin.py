#!/usr/bin/env python3
"""Fail closed unless every active Gate_SDK declaration is the moving major tag.

Gate_SDK owns release identity (``contracts/RELEASE_IDENTITY_LEDGER.json``,
schema v2). The consumer compatibility contract is the moving major channel
``v1`` — not a commit sha.

EIE declared the SDK in three executable places and they disagreed:
``pyproject.toml`` and ``requirements-ci.txt`` named 69c6c67 while the
packet/envelope gate step in ``.github/workflows/pr-pipeline.yml`` installed
ead0f48. The old validator read four manifests and never the workflow, so the
third identity was invisible to it. Every surface that installs the SDK is
enumerated here, and a missing one FAILs rather than being skipped.

Reproducibility lives in the generated ``requirements.lock``, which records the
concrete object the channel resolved to plus that archive's sha256. That object
is execution evidence; it is never the declared dependency, and this validator
never requires a particular value for it.

Modes
-----
default        Offline structural checks over every active surface.
--verify-tag   Resolves ``v1`` at the canonical remote and requires the lock's
               resolved object to match. Fails closed when the remote cannot be
               resolved — a lock that cannot be checked has not been checked.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAJOR_TAG = "v1"
CANONICAL_REPO = "Quantum-L9/Gate_SDK"
CANONICAL_REMOTE = f"https://github.com/{CANONICAL_REPO}.git"
FORBIDDEN_FORK = "cryptoxdog/Gate_SDK"
FORBIDDEN_BRANCHES = ("main", "master")

# Every surface that declares or directly installs the SDK. The workflow is
# on this list because leaving it off is what let a third sha live in CI.
MANIFESTS = (
    "pyproject.toml",
    "requirements-ci.txt",
    ".github/workflows/pr-pipeline.yml",
)
LOCK = "requirements.lock"

SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")
MANIFEST_REF_RE = re.compile(r"Gate_SDK(?:\.git)?@([0-9A-Za-z._/-]+)")
LOCK_ARCHIVE_RE = re.compile(
    r"constellation-node-sdk @ https://github\.com/"
    + re.escape(CANONICAL_REPO)
    + r"/archive/(?P<sha>[0-9a-f]{40})\.tar\.gz\s+--hash=sha256:(?P<digest>[0-9a-f]{64})"
)


def resolve_remote_tag(remote: str, tag: str) -> str | None:
    """Resolve ``refs/tags/<tag>`` at *remote*, preferring the peeled object."""
    completed = subprocess.run(
        ["/usr/bin/git", "ls-remote", "--tags", remote, f"refs/tags/{tag}"],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        return None
    peeled: str | None = None
    direct: str | None = None
    for line in completed.stdout.splitlines():
        sha, _, name = line.partition("\t")
        if name == f"refs/tags/{tag}^{{}}":
            peeled = sha.strip()
        elif name == f"refs/tags/{tag}":
            direct = sha.strip()
    return peeled or direct


def check_manifest(rel: str, text: str) -> list[str]:
    """An active surface declares the compatibility channel and nothing else."""
    errors: list[str] = []
    if FORBIDDEN_FORK in text:
        errors.append(f"{rel}: forbidden fork {FORBIDDEN_FORK}")

    refs = MANIFEST_REF_RE.findall(text)
    if not refs:
        errors.append(f"{rel}: no {CANONICAL_REPO} declaration found")
        return errors

    for ref in refs:
        if ref == MAJOR_TAG:
            continue
        if SHA_RE.fullmatch(ref):
            errors.append(
                f"{rel}: Gate_SDK declared by commit sha {ref} — the consumer "
                f"contract is the moving major channel {MAJOR_TAG}"
            )
        elif ref in FORBIDDEN_BRANCHES:
            errors.append(f"{rel}: Gate_SDK floats on branch {ref!r}")
        else:
            errors.append(f"{rel}: Gate_SDK ref {ref!r} is not {MAJOR_TAG!r}")

    if CANONICAL_REPO not in text:
        errors.append(f"{rel}: missing canonical repo {CANONICAL_REPO}")
    return errors


def lock_resolution(text: str) -> str | None:
    """The concrete object the generated lock resolved the channel to."""
    match = LOCK_ARCHIVE_RE.search(text)
    return match.group("sha") if match else None


def check_lock(rel: str, text: str) -> list[str]:
    """The lock must carry a concrete, hash-verified archive."""
    if FORBIDDEN_FORK in text:
        return [f"{rel}: forbidden fork {FORBIDDEN_FORK}"]
    if lock_resolution(text) is None:
        return [
            f"{rel}: no hash-verified {CANONICAL_REPO} archive at a concrete "
            "40-character object — regenerate with tools/lock_requirements.sh"
        ]
    return []


def check_tree(root: Path) -> list[str]:
    errors: list[str] = []
    for rel in MANIFESTS:
        path = root / rel
        if not path.is_file():
            errors.append(f"{rel}: active surface is missing")
            continue
        errors.extend(check_manifest(rel, path.read_text(encoding="utf-8")))

    lock_path = root / LOCK
    if not lock_path.is_file():
        errors.append(f"{LOCK}: generated lock is missing")
    else:
        errors.extend(check_lock(LOCK, lock_path.read_text(encoding="utf-8")))
    return errors


def compare_lock_to_tag(resolved: str | None, tag_sha: str | None) -> list[str]:
    """Stale-lock detection: the lock must hold what the channel points at now."""
    if tag_sha is None:
        return [
            f"--verify-tag: could not resolve {CANONICAL_REPO}@{MAJOR_TAG}; "
            "an unverifiable lock does not pass"
        ]
    if resolved is None:
        return ["--verify-tag: lock carries no concrete resolved object to compare"]
    if resolved != tag_sha:
        return [
            f"--verify-tag: lock resolves {resolved} but {MAJOR_TAG} now points at "
            f"{tag_sha} — the lock is stale; regenerate it"
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the Gate_SDK moving-major pin.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--verify-tag",
        action="store_true",
        help="resolve the channel at the canonical remote and compare to the lock",
    )
    parser.add_argument("--remote", default=CANONICAL_REMOTE)
    args = parser.parse_args(argv)

    errors = check_tree(args.root)

    if args.verify_tag:
        lock_path = args.root / LOCK
        resolved = (
            lock_resolution(lock_path.read_text(encoding="utf-8")) if lock_path.is_file() else None
        )
        tag_sha = resolve_remote_tag(args.remote, MAJOR_TAG)
        errors.extend(compare_lock_to_tag(resolved, tag_sha))
        if not errors:
            print(f"NETWORK: {CANONICAL_REPO}@{MAJOR_TAG} == lock {tag_sha}")

    if errors:
        print("FAIL")
        print("\n".join(errors))
        return 1

    mode = "offline + networked" if args.verify_tag else "offline"
    print(f"PASS EIE declares {CANONICAL_REPO}@{MAJOR_TAG} ({mode})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
