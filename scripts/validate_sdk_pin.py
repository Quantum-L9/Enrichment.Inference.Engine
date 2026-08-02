#!/usr/bin/env python3
"""Fail closed unless Gate_SDK pin matches the immutable constellation SHA."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = "a770e8531dc1c59ce01e1dbb0f4162785d9dda89"
errors: list[str] = []

for rel in ["pyproject.toml", "requirements.txt", "poetry.lock"]:
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
