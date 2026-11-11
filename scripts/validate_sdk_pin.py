#!/usr/bin/env python3
"""Fail closed unless Gate_SDK is declared on the moving major tag and locked.

Gate_SDK owns release identity (``contracts/RELEASE_IDENTITY_LEDGER.json``,
schema v2). The consumer compatibility contract is the moving major channel
``v1`` — not a commit sha.

EIE once declared the SDK in three executable places and they disagreed:
``pyproject.toml`` and the CI requirements file named 69c6c67 while the
packet/envelope gate step in ``.github/workflows/pr-pipeline.yml`` installed
ead0f48 (EIE-001). The fix is structural: there is now one declaration and one
lock, and every install site reads the lock.

* ``pyproject.toml`` declares ``constellation-node-sdk`` by name, and
  ``[tool.uv.sources]`` names the canonical repository at tag ``v1``.
* ``uv.lock`` records that tag in the source URL query and the resolved commit
  in its fragment: ``git = "https://github.com/Quantum-L9/Gate_SDK.git?tag=v1#<commit>"``.
  That commit is execution evidence; it is never the declared dependency, and
  this validator never requires a particular value for it.
* No workflow, Dockerfile or devcontainer installs the SDK directly: each
  installs from uv.lock (``tools/deps/install_locked.sh``). A direct install
  is a second identity, so finding one FAILs.

Modes
-----
default        Offline structural checks over the manifest, the lock and every
               install surface.
--verify-tag   Resolves ``v1`` at the canonical remote and requires the lock's
               resolved commit to match. Fails closed when the remote cannot be
               resolved — a lock that cannot be checked has not been checked.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAJOR_TAG = "v1"
SDK = "constellation-node-sdk"
CANONICAL_REPO = "Quantum-L9/Gate_SDK"
CANONICAL_REMOTE = f"https://github.com/{CANONICAL_REPO}.git"
FORBIDDEN_FORK = "cryptoxdog/Gate_SDK"

MANIFEST = "pyproject.toml"
LOCK = "uv.lock"
# Files that install packages. None may name the SDK as a direct install: the
# lock is the only place its identity is recorded.
INSTALL_SURFACE_GLOBS = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    "Dockerfile*",
    ".devcontainer/*",
)

SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")
# The constellation-node-sdk entry of uv.lock: its git source carries the
# requested tag in the query and the resolved commit in the fragment.
LOCK_REFERENCE_RE = re.compile(
    r'name = "constellation-node-sdk"\nversion = "[^"]*"\n'
    r'source = \{ git = "(?P<url>[^"?#]+)\?(?P<kind>[a-z]+)=(?P<ref>[^"#&]+)#(?P<sha>[0-9a-f]{40})" \}'
)
DIRECT_INSTALL_RE = re.compile(r"Gate_SDK(?:\.git)?@|Gate_SDK/archive/|constellation-node-sdk\s*@")


def safe_remote(remote: str) -> str:
    """Return a remote from a fixed set — never the caller's string.

    Passing argv as a list and never invoking a shell stops *command*
    injection, but not *argument* injection: ``git ls-remote
    --upload-pack=<cmd> <repo>`` runs ``<cmd>``, so a remote beginning with
    ``-`` is an execution vector on its own (SonarCloud
    pythonsecurity:S8705).

    The canonical remote is the contract, so it is matched by equality and the
    module constant is returned: what reaches git is provably not built from
    the argument. Anything else is a local fixture path used by the tests,
    which must be an existing git repository directory. The CLI deliberately
    exposes no --remote flag — an operator pointing this check at a
    non-canonical repository is exactly what the release-identity contract
    exists to prevent.
    """
    if remote == CANONICAL_REMOTE:
        return CANONICAL_REMOTE
    if not remote or remote.startswith("-"):
        msg = f"refusing Gate_SDK remote {remote!r}: a remote must not begin with '-'"
        raise ValueError(msg)
    candidate = Path(remote)
    if not candidate.is_dir():
        msg = f"refusing Gate_SDK remote {remote!r}: not canonical and not a local repository"
        raise ValueError(msg)
    return str(candidate.resolve(strict=True))


def resolve_remote_tag(remote: str, tag: str) -> str | None:
    """Resolve ``refs/tags/<tag>`` at *remote*, preferring the peeled object."""
    completed = subprocess.run(
        [
            "/usr/bin/git",
            "ls-remote",
            "--tags",
            "--end-of-options",
            safe_remote(remote),
            f"refs/tags/{tag}",
        ],
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


def _declaration_errors(rel: str, data: dict) -> list[str]:
    """[project] names the SDK bare; its source lives in [tool.uv.sources]."""
    declared = [
        spec
        for spec in data.get("project", {}).get("dependencies", [])
        if re.match(rf"{re.escape(SDK)}\b", spec.strip())
    ]
    if not declared:
        return [f"{rel}: {SDK} is not a [project] dependency"]
    return [
        f"{rel}: {SDK} declared as {spec!r}; name it bare and source it through [tool.uv.sources]"
        for spec in declared
        if spec.strip() != SDK
    ]


def _source_errors(rel: str, source: dict) -> list[str]:
    """The [tool.uv.sources] entry is the canonical repository on tag v1."""
    errors: list[str] = []
    if source.get("git") != CANONICAL_REMOTE:
        errors.append(f"{rel}: {SDK} source is {source.get('git')!r}, not {CANONICAL_REMOTE}")
    if "rev" in source:
        rev = source["rev"]
        kind = "commit sha" if SHA_RE.fullmatch(str(rev)) else "rev"
        errors.append(
            f"{rel}: Gate_SDK declared by {kind} {rev} — the consumer contract "
            f"is the moving major channel {MAJOR_TAG}"
        )
    if "branch" in source:
        errors.append(f"{rel}: Gate_SDK floats on branch {source['branch']!r}")
    tag = source.get("tag")
    if tag is None:
        if "rev" not in source and "branch" not in source:
            errors.append(f"{rel}: {SDK} source names no tag (want {MAJOR_TAG!r})")
    elif tag != MAJOR_TAG:
        errors.append(f"{rel}: Gate_SDK ref {tag!r} is not {MAJOR_TAG!r}")
    return errors


def check_manifest(rel: str, text: str) -> list[str]:
    """pyproject.toml names the SDK and sources it from the canonical tag v1."""
    if FORBIDDEN_FORK in text:
        return [f"{rel}: forbidden fork {FORBIDDEN_FORK}"]
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [f"{rel}: not valid TOML ({exc})"]

    errors = _declaration_errors(rel, data)
    source = data.get("tool", {}).get("uv", {}).get("sources", {}).get(SDK)
    if source is None:
        errors.append(f"{rel}: no [tool.uv.sources] entry for {SDK}")
        return errors
    return errors + _source_errors(rel, source)


def lock_resolution(text: str) -> str | None:
    """The concrete commit uv.lock recorded for the moving tag."""
    match = LOCK_REFERENCE_RE.search(text)
    return match.group("sha") if match else None


def check_lock(rel: str, text: str) -> list[str]:
    """uv.lock carries the canonical source on tag v1 at a concrete commit."""
    if FORBIDDEN_FORK in text:
        return [f"{rel}: forbidden fork {FORBIDDEN_FORK}"]
    match = LOCK_REFERENCE_RE.search(text)
    if match is None:
        return [
            f"{rel}: no {CANONICAL_REPO} git source at a concrete 40-character "
            "commit — run `uv lock`"
        ]
    errors: list[str] = []
    if match.group("url") != CANONICAL_REMOTE:
        errors.append(f"{rel}: {SDK} locked from {match.group('url')}, not {CANONICAL_REMOTE}")
    if (match.group("kind"), match.group("ref")) != ("tag", MAJOR_TAG):
        errors.append(
            f"{rel}: {SDK} locked from {match.group('kind')}={match.group('ref')!r} "
            f"(want tag={MAJOR_TAG!r})"
        )
    return errors


def check_install_surface(rel: str, text: str) -> list[str]:
    """An install site installs from uv.lock, never the SDK directly."""
    errors: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if DIRECT_INSTALL_RE.search(line):
            errors.append(
                f"{rel}:{number}: installs Gate_SDK directly — install it from "
                "uv.lock (tools/deps/install_locked.sh) so it is the locked commit"
            )
    return errors


def check_tree(root: Path) -> list[str]:
    errors: list[str] = []
    for rel, check in ((MANIFEST, check_manifest), (LOCK, check_lock)):
        path = root / rel
        if not path.is_file():
            errors.append(f"{rel}: missing")
            continue
        errors.extend(check(rel, path.read_text(encoding="utf-8")))

    surfaces = sorted(
        {path for pattern in INSTALL_SURFACE_GLOBS for path in root.glob(pattern) if path.is_file()}
    )
    for path in surfaces:
        rel = path.relative_to(root).as_posix()
        errors.extend(check_install_surface(rel, path.read_text(encoding="utf-8")))
    return errors


def compare_lock_to_tag(resolved: str | None, tag_sha: str | None) -> list[str]:
    """Stale-lock detection: the lock must hold what the channel points at now."""
    if tag_sha is None:
        return [
            f"--verify-tag: could not resolve {CANONICAL_REPO}@{MAJOR_TAG}; "
            "an unverifiable lock does not pass"
        ]
    if resolved is None:
        return ["--verify-tag: uv.lock carries no concrete resolved commit to compare"]
    if resolved != tag_sha:
        return [
            f"--verify-tag: uv.lock resolves {resolved} but {MAJOR_TAG} now points at "
            f"{tag_sha} — the lock is stale; run `uv lock --upgrade-package {SDK}`"
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the Gate_SDK moving-major pin.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--verify-tag",
        action="store_true",
        help="resolve the channel at the canonical remote and compare to uv.lock",
    )
    args = parser.parse_args(argv)

    errors = check_tree(args.root)
    lock_path = args.root / LOCK
    resolved = (
        lock_resolution(lock_path.read_text(encoding="utf-8")) if lock_path.is_file() else None
    )

    if args.verify_tag:
        try:
            tag_sha = resolve_remote_tag(CANONICAL_REMOTE, MAJOR_TAG)
        except ValueError as exc:
            # Fails closed like any other unresolvable channel, but says which
            # of the two reasons it was.
            errors.append(f"--verify-tag: {exc}")
        else:
            errors.extend(compare_lock_to_tag(resolved, tag_sha))
            if not errors:
                print(f"NETWORK: {CANONICAL_REPO}@{MAJOR_TAG} == uv.lock {tag_sha}")

    if errors:
        print("FAIL")
        print("\n".join(errors))
        return 1

    mode = "offline + networked" if args.verify_tag else "offline"
    print(f"PASS EIE declares {CANONICAL_REPO}@{MAJOR_TAG} ({mode}) -> {resolved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
