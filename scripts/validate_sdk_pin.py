#!/usr/bin/env python3
"""Fail closed unless Gate_SDK pin matches the immutable constellation SHA."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The exact Gate_SDK revision the whole EIE <-> CEG seam runs: main a0827f2
# plus the env-config fix that loads L9_VERIFYING_KEYS_JSON into GateClientConfig
# (69c6c67; without it a signature-requiring node rejects every signed Gate
# response). Gate and CEG pin the same commit, so every process on the rail
# runs one SDK.
PIN = "69c6c67060b08440734a61473c03663423709964"
errors: list[str] = []

# requirements-ci.txt is where CI resolves the SDK from; omitting it let the
# two pin sites drift without the validator noticing.
for rel in ["pyproject.toml", "requirements.txt", "requirements-ci.txt", "poetry.lock"]:
    path = ROOT / rel
    if not path.exists():
        continue
    text = path.read_text()
    if "Quantum-L9/Gate_SDK" not in text:
        errors.append(f"{rel}: missing Quantum-L9")
    if PIN not in text:
        errors.append(f"{rel}: missing pin")
    if "cryptoxdog/Gate_SDK" in text:
        errors.append(f"{rel}: cryptoxdog remains")

if errors:
    print("FAIL")
    print("\n".join(errors))
    raise SystemExit(1)

print("PASS EIE pin", PIN)
