#!/usr/bin/env python3
"""Fail closed unless every governed Gate_SDK declaration names one immutable SHA."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The exact Gate_SDK revision the whole EIE <-> CEG seam runs: main a0827f2
# plus the env-config fix that loads L9_VERIFYING_KEYS_JSON into GateClientConfig
# (69c6c67; without it a signature-requiring node rejects every signed Gate
# response). Gate and CEG pin the same commit, so every process on the rail
# runs one SDK.
PIN = "69c6c67060b08440734a61473c03663423709964"

# Every file that resolves Gate_SDK for a running process. requirements-ci.txt is
# where CI resolves the SDK from, and pr-pipeline.yml installs the SDK a second
# time for the packet/envelope gates; omitting either let those pin sites drift
# without the validator noticing.
GOVERNED = [
    "pyproject.toml",
    "requirements.txt",
    "requirements-ci.txt",
    "poetry.lock",
    ".github/workflows/pr-pipeline.yml",
]

# Known gap, tracked outside this guard: requirements.lock (and the
# Dockerfile.prod --require-hashes install it feeds) carries the hashed source
# archive for release commit 2b2f53a28a59bbfb2fa45f5eac32b722d802209a, which is
# NOT PIN. pip cannot hash-verify a git+https URL, so the lock rewrites the pin
# to a tarball URL + sha256 via tools/lock_requirements.sh. Closing that split
# means regenerating the lock, so it is deliberately not asserted here rather
# than asserted and left failing.
PRODUCTION_LOCK_SHA = "2b2f53a28a59bbfb2fa45f5eac32b722d802209a"

# Matches a Gate_SDK git ref in any governed declaration: the `@<ref>` that pip
# resolves. `<ref>` runs to the first quote, whitespace, comma or bracket.
REF_RE = re.compile(r"Quantum-L9/Gate_SDK(?:\.git)?@([^\"'\s,\]]+)")
SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")

errors: list[str] = []

for rel in GOVERNED:
    path = ROOT / rel
    if not path.exists():
        continue
    text = path.read_text()
    if "cryptoxdog/Gate_SDK" in text:
        errors.append(f"{rel}: cryptoxdog remains")
    if "Quantum-L9/Gate_SDK" not in text:
        errors.append(f"{rel}: missing Quantum-L9")
        continue

    refs = REF_RE.findall(text)
    if not refs:
        errors.append(f"{rel}: no Gate_SDK git ref found")
        continue
    for ref in refs:
        if not SHA_RE.match(ref):
            # A branch or tag is mutable, so it cannot be a release identity:
            # the ref can be repointed at another commit without an EIE change.
            errors.append(f"{rel}: floating ref @{ref} (need immutable 40-char SHA)")
        elif ref != PIN:
            errors.append(f"{rel}: pin @{ref} != {PIN}")

if errors:
    print("FAIL")
    print("\n".join(errors))
    raise SystemExit(1)

print("PASS EIE pin", PIN)
